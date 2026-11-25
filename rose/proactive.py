"""Rose speaks up on her own (rose-watch, every tick). Config proactive_enabled turns all of it off.

* Meetings: "Stand-up in 10 minutes" (meeting_alert_minutes before; calendar from `rose agenda setup`).
* Failed CI runs: any failed GitHub Actions run on your recently pushed repos (ci_runs_watch), including
  pushes to main with no PR. PR checks are already covered by rose.watch.
* Low battery: laptop (alert_battery_percent, rose.watch pushes; Rose also says it) and the phone
  (phone_battery_alert_percent, read over ADB while the phone is linked).
* Breaks: after break_reminder_minutes of activity with no 5-minute pause, a reminder; again every
  break_reminder_minutes until you take one.
Each alert: desktop notification + phone push + spoken on the laptop (proactive_speak, not in quiet_hours).
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from datetime import datetime

from rose.common import _log
from rose.config import load_config
from rose.push import push

BREAK_IDLE = 300        # an idle stretch this long counts as a break
CI_EVERY = 600
PHONE_BATTERY_EVERY = 300
MEETING_EVERY = 60


def in_quiet_hours(now: datetime | None = None, spec: str | None = None) -> bool:
    spec = load_config().get("quiet_hours", "22:00-08:00") if spec is None else spec
    if not spec or "-" not in spec:
        return False
    try:
        a, b = (tuple(map(int, x.strip().split(":"))) for x in spec.split("-", 1))
    except ValueError:
        return False
    t = ((now or datetime.now()).hour, (now or datetime.now()).minute)
    return a <= t < b if a <= b else (t >= a or t < b)


def _say(text: str) -> None:
    if not shutil.which("spd-say"):
        return
    try:
        subprocess.run(["spd-say", "--wait", "--", text[:300]], timeout=60, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


def announce(title: str, message: str, spoken: str = "", priority: str = "default", tags: str = "",
             url: str | None = None, speak: bool = True) -> None:
    """Phone push + desktop notification (both via push()) + (optionally) say it out loud."""
    push(title, message, priority=priority, tags=tags, url=url)
    cfg = load_config()
    if speak and cfg.get("proactive_speak", True) and not in_quiet_hours():
        threading.Thread(target=_say, args=(spoken or f"{title}. {message}",), daemon=True).start()
    _log({"event": "proactive", "title": title, "message": message[:200]})


# ---------------- meetings ----------------

def tick_meetings(state: dict, now: datetime | None = None) -> list[str]:
    mins = float(load_config().get("meeting_alert_minutes", 10) or 0)
    if mins <= 0:
        return []
    from rose import agenda
    now = now or datetime.now()
    told = state.setdefault("meetings_announced", {})
    out = []
    for e in agenda.upcoming(mins, now):
        key = f"{e['start']:%Y-%m-%dT%H:%M}|{e['title']}"
        if key in told:
            continue
        told[key] = now.isoformat(timespec="minutes")
        left = max(1, round((e["start"] - now).total_seconds() / 60))
        where = f" ({e['location']})" if e.get("location") else ""
        announce(f"📅 {e['title']} in {left} min", f"{e['start']:%H:%M}-{e['end']:%H:%M}{where}",
                 spoken=f"Heads up: {e['title']} starts in {left} minutes.", priority="high", tags="calendar")
        out.append(key)
    today = now.strftime("%Y-%m-%d")
    state["meetings_announced"] = {k: v for k, v in told.items() if k[:10] >= today}
    return out


# ---------------- CI runs ----------------

def _gh(path: str):
    from rose.watch import _gh_api
    return _gh_api(path)


def tick_ci_runs(state: dict) -> list[dict]:
    """Failed workflow runs on repos you pushed to in the last 3 days. Seeds silently on first look."""
    if not load_config().get("ci_runs_watch", True) or not shutil.which("gh"):
        return []
    from rose.watch import _gh_login
    me = _gh_login(state)
    if not me:
        return []
    cutoff = time.time() - 3 * 86400
    repos = []
    for r in _gh("user/repos?sort=pushed&per_page=10&affiliation=owner,collaborator") or []:
        try:
            pushed = datetime.fromisoformat(str(r.get("pushed_at", "")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if pushed >= cutoff and r.get("full_name"):
            repos.append(r["full_name"])
    seen = state.setdefault("ci_runs_seen", {})
    first = not state.get("ci_runs_started")
    out = []
    for repo in repos[:8]:
        try:
            runs = (_gh(f"repos/{repo}/actions/runs?per_page=10&actor={me}") or {}).get("workflow_runs", [])
        except Exception:
            continue
        mark = seen.get(repo, 0)
        newest = max([mark] + [int(r.get("id", 0)) for r in runs if r.get("status") == "completed"])
        if repo in seen and not first:
            for r in runs:
                if int(r.get("id", 0)) > mark and r.get("status") == "completed" \
                        and r.get("conclusion") in ("failure", "timed_out", "startup_failure"):
                    out.append(r)
                    announce(f"❌ CI failed: {repo.split('/')[-1]}",
                             f"{r.get('name', 'workflow')} on {r.get('head_branch', '?')}: "
                             f"{(r.get('display_title') or '')[:80]}",
                             spoken=f"CI failed on {repo.split('/')[-1]}, {r.get('head_branch', '')} branch.",
                             priority="high", tags="x", url=r.get("html_url"))
        seen[repo] = newest
    state["ci_runs_started"] = True
    return out


# ---------------- battery ----------------

def tick_laptop_battery(state: dict) -> bool:
    """Say it out loud when the laptop is low (rose.watch already pushes the alert)."""
    import psutil
    bat = psutil.sensors_battery()
    limit = load_config().get("alert_battery_percent", 15)
    if not bat or bat.power_plugged or not (0 < bat.percent <= limit):
        state["laptop_battery_told"] = False
        return False
    if state.get("laptop_battery_told"):
        return False
    state["laptop_battery_told"] = True
    if load_config().get("proactive_speak", True) and not in_quiet_hours():
        threading.Thread(target=_say, args=(f"Battery is at {bat.percent:.0f} percent. Please plug in the laptop.",),
                         daemon=True).start()
    return True


def tick_phone_battery(state: dict) -> bool:
    limit = float(load_config().get("phone_battery_alert_percent", 15) or 0)
    if limit <= 0 or not shutil.which("adb"):
        return False
    from rose.tools import phone
    if phone._read_last().get("_watch_via") in (None, "offline"):
        return False
    try:
        from rose.tools.phone_control import phone_status
        b = phone_status().get("battery", {})
    except Exception:
        return False
    level, st = b.get("level"), b.get("state")
    if level is None or st in ("charging", "full") or level > limit:
        if level is not None and level > limit + 5:
            state["phone_battery_told"] = False  # recharged: may alert again next time
        return False
    if state.get("phone_battery_told"):
        return False
    state["phone_battery_told"] = True
    announce("🔋 Phone battery low", f"{level}% and not charging.",
             spoken=f"Your phone battery is at {level} percent.", priority="high", tags="battery")
    return True


# ---------------- breaks ----------------

def tick_break(state: dict, idle: float | None = None, now: float | None = None) -> bool:
    mins = float(load_config().get("break_reminder_minutes", 120) or 0)
    if mins <= 0:
        return False
    now = now or time.time()
    if idle is None:
        from rose import activity
        idle = activity.idle_seconds()
        if idle is not None and activity.is_locked():
            idle = max(idle, BREAK_IDLE)
    if idle is None:
        return False
    if idle >= BREAK_IDLE:
        state["active_since"] = None  # that was a break
        return False
    since = state.get("active_since") or (now - idle)
    state["active_since"] = since
    if now - since < mins * 60:
        return False
    state["active_since"] = now  # next reminder after another full stretch
    hours = (now - since) / 3600
    span = f"{hours:.1f} hours".replace(".0 hours", " hours") if hours >= 1 else f"{(now - since) / 60:.0f} minutes"
    announce("☕ Time for a break", f"You've been at it for {span}. Stand up, stretch, drink some water.",
             spoken=f"You've been working for {span}. Time for a short break.", tags="coffee")
    return True


# ---------------- loop hook ----------------

def tick(state: dict) -> None:
    """Called from rose.watch every 30 s; each check runs at its own pace."""
    if not load_config().get("proactive_enabled", True):
        return
    from rose.watch import _safe
    now = time.time()
    last = state.setdefault("proactive_last", {})

    def due(name: str, every: float) -> bool:
        if now - last.get(name, 0) >= every:
            last[name] = now
            return True
        return False

    _safe("break", tick_break, state)
    if due("meetings", MEETING_EVERY):
        _safe("meetings", tick_meetings, state)
    if due("laptop_battery", 60):
        _safe("laptop_battery", tick_laptop_battery, state)
    if due("phone_battery", PHONE_BATTERY_EVERY):
        _safe("phone_battery", tick_phone_battery, state)
    if due("ci_runs", CI_EVERY):
        _safe("ci_runs", tick_ci_runs, state)
