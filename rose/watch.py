"""`rose watch`: the proactive part of ROSE. Runs as rose-watch.service.

Every 30 s: due schedules (reminders, reports, scheduled Forge tasks), new security events, the work-log
sampler (rose.activity) and context triggers (rose.triggers). Every 5 min: disk, RAM, battery.
Every 10 min: GitHub (CI failing / passing again on your PRs, new reviews and review comments on them,
new review requests).
Daily: cleanup of old screenshots/logs. Weekly: failure report (rose.failures). Proactive alerts (meetings,
failed CI runs, phone battery, breaks) come from rose.proactive. Alerts go to the phone (ntfy) and the desktop.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from typing import Literal

from rose.common import ToolError, _log
from rose.config import LOG_DIR, ROSE_HOME, load_config
from rose.push import push

STATE = ROSE_HOME / "watch_state.json"
SCHEDULES = ROSE_HOME / "schedules.json"
_lock = threading.Lock()
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _load(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _save(path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


def _cooldown(state: dict, key: str, seconds: float) -> bool:
    """True if `key` may alert now (and records it)."""
    now = time.time()
    if now - state.setdefault("cooldowns", {}).get(key, 0) < seconds:
        return False
    state["cooldowns"][key] = now
    return True


# ---------------- schedules ----------------

def _next_run(item: dict, after: datetime) -> datetime | None:
    h, m = map(int, item["time"].split(":"))
    if item["repeat"] == "once":
        d = datetime.strptime(item["date"], "%Y-%m-%d") if item.get("date") else after
        t = d.replace(hour=h, minute=m, second=0, microsecond=0)
        if not item.get("date") and t <= after:
            t += timedelta(days=1)  # "at 18:00" said after 18:00 means tomorrow
        return t
    t = after.replace(hour=h, minute=m, second=0, microsecond=0)
    if t <= after:
        t += timedelta(days=1)
    for _ in range(8):
        wd = t.weekday()
        if item["repeat"] == "daily" or (item["repeat"] == "weekdays" and wd < 5) or \
                (item["repeat"] == "weekly" and WEEKDAYS[wd] == item.get("weekday")):
            return t
        t += timedelta(days=1)
    return None


def schedule_add(kind: Literal["remind", "report", "forge", "routine"], time_hhmm: str, text: str = "",
                 repeat: Literal["once", "daily", "weekdays", "weekly"] = "once", date: str = "",
                 weekday: str = "", confirm: bool = False) -> dict:
    """Schedule: remind (push text to phone), report (system+GitHub summary to bridge), forge (run text as a
    Forge task; needs confirm=true), routine (text = routine name, e.g. "work"). time_hhmm "18:00";
    date YYYY-MM-DD; repeat once|daily|weekdays|weekly."""
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", time_hhmm.strip()):
        raise ToolError("time_hhmm must be 24-hour HH:MM, e.g. 09:00 or 18:30.")
    if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ToolError("date must be YYYY-MM-DD.")
    if repeat == "weekly" and weekday.lower()[:3] not in WEEKDAYS:
        raise ToolError("weekly needs weekday: mon..sun.")
    if kind == "routine":
        from rose.tools import routines

        if text.strip().lower().removesuffix(" mode").strip() not in routines.list_routines():
            raise ToolError(f"No routine '{text}'. Routines: {', '.join(routines.list_routines())}.")
    if kind in ("remind", "forge", "routine") and not text.strip():
        raise ToolError("Say what to remind about / what Forge should do.")
    if len(text) > 2000:
        raise ToolError("Text too long (max 2000).")
    if kind == "forge" and not confirm:
        raise ToolError("Scheduled Forge tasks run unattended on the work account. Ask the user, then call "
                        "again with confirm=true.")
    from rose.guard import caller
    item = {"id": secrets.token_hex(3), "kind": kind, "time": time_hhmm.strip().zfill(5), "text": text.strip(),
            "repeat": repeat, "date": date, "weekday": weekday.lower()[:3], "created_by": caller(),
            "created": datetime.now().isoformat(timespec="seconds")}
    nxt = _next_run(item, datetime.now())
    if nxt is None or nxt < datetime.now():
        raise ToolError("That time is in the past.")
    item["next_run"] = nxt.isoformat(timespec="minutes")
    with _lock:
        items = _load(SCHEDULES, [])
        if len(items) >= 50:
            raise ToolError("Too many schedules (50). Remove some first.")
        items.append(item)
        _save(SCHEDULES, items)
    _log({"event": "schedule_added", **{k: item[k] for k in ("id", "kind", "repeat", "next_run", "created_by")}})
    return {"scheduled": item["id"], "next_run": item["next_run"], "kind": kind, "repeat": repeat}


def schedule_list() -> list[dict]:
    """All scheduled reminders, reports and Forge tasks, with their next run time."""
    return [{k: i.get(k) for k in ("id", "kind", "text", "time", "repeat", "weekday", "next_run", "created_by",
                                   "last_run")} for i in _load(SCHEDULES, [])] or [{"none": "Nothing scheduled."}]


def schedule_remove(schedule_id: str) -> str:
    """Delete a schedule by id (see schedule_list)."""
    with _lock:
        items = _load(SCHEDULES, [])
        keep = [i for i in items if i["id"] != schedule_id]
        if len(keep) == len(items):
            raise ToolError(f"No schedule {schedule_id}.")
        _save(SCHEDULES, keep)
    return f"Removed schedule {schedule_id}."


def _report_text() -> tuple[str, str]:
    from rose.tools import system
    s = system.system_status(3)
    lines = [f"CPU {s['cpu']['percent']:.0f}% · RAM {s['memory']['percent']:.0f}% · "
             f"disk {s['disk_root']['percent']:.0f}% ({s['disk_root']['free_gb']} GB free)"]
    short = lines[0]
    try:
        from rose.tools import github
        prs = github.github_my_prs()
        lines.append(f"Your open PRs: {len(prs['authored'])}; waiting for your review: {len(prs['review_requested'])}")
        for p in prs["authored"][:5]:
            st = github.github_pr_status(p["repo"], p["number"])
            lines.append(f"  #{p['number']} {p['title'][:60]} [{p['repo']}] review={st['review_decision'] or '-'}"
                         f" failing={','.join(st['failing']) or 'none'}")
        for p in prs["review_requested"][:5]:
            lines.append(f"  review: #{p['number']} {p['title'][:60]} [{p['repo']}]")
        short += f" · PRs {len(prs['authored'])} · reviews {len(prs['review_requested'])}"
    except Exception as e:
        lines.append(f"GitHub: unavailable ({type(e).__name__})")
    return short, "\n".join(lines)


def _run_schedule(item: dict) -> None:
    from rose import guard
    from rose.tools.bridge import post_as
    if guard.lockdown() and item.get("kind") != "remind":  # lockdown stops unattended actions too
        _log({"event": "schedule_skipped_lockdown", "kind": item.get("kind")})
        return
    try:
        if item["kind"] == "remind":
            push("⏰ Reminder", item["text"], priority="high", tags="alarm_clock")
        elif item["kind"] == "routine":
            from rose.tools import routines
            push("🔁 Routine", routines.run_routine(item["text"]), tags="repeat")
        elif item["kind"] == "report":
            short, full = _report_text()
            post_as("ROSE", f"Scheduled report ({datetime.now():%a %d %b %H:%M})\n\n{full}", "all", email=False)
            push("ROSE report", short + "\nFull report on the bridge.", tags="bar_chart")
        elif item["kind"] == "forge":
            from rose.tools import forge
            job = forge._launch("task", item["text"], forge.READ_TOOLS, forge.DENY_TOOLS, forge.SYSTEM,
                                {"task": item["text"], "scheduled": item["id"]})
            rep = forge._report(job, 0)
            deadline = time.time() + forge._cfg()["timeout"] + 60
            while rep["status"] == "running" and time.time() < deadline:
                time.sleep(10)
                rep = forge._report(job, 0)
            post_as("Forge (scheduled)", f"Task: {item['text']}\n\n{rep.get('result') or rep.get('error')}"
                    + (f"\n\nDrafts waiting for approval: {rep['drafts']}" if rep.get("drafts") else ""), "all",
                    email=False)
            push("Forge task finished" if rep["status"] == "done" else "Forge task failed",
                 f"\"{item['text'][:80]}\": result on the bridge.", tags="hammer")
        _log({"event": "schedule_ran", "id": item["id"], "kind": item["kind"]})
    except Exception as e:
        _log({"event": "schedule_failed", "id": item["id"], "error": f"{type(e).__name__}: {e}"})


def _tick_schedules() -> None:
    now = datetime.now()
    due = []
    with _lock:
        items = _load(SCHEDULES, [])
        keep = []
        for i in items:
            if datetime.fromisoformat(i["next_run"]) <= now:
                due.append(dict(i))
                i["last_run"] = now.isoformat(timespec="minutes")
                if i["repeat"] == "once":
                    continue
                nxt = _next_run(i, now + timedelta(minutes=1))
                i["next_run"] = nxt.isoformat(timespec="minutes") if nxt else i["next_run"]
            keep.append(i)
        if due:
            _save(SCHEDULES, keep)
    for item in due:
        threading.Thread(target=_run_schedule, args=(item,), daemon=True).start()


# ---------------- watchers ----------------

SECURITY_EVENTS = {
    "injection_suspected": ("⚠️ Possible prompt injection", "high"),
    "oauth_login_failed": ("⚠️ Wrong passphrase on ROSE login", "high"),
    "approval_wrong_passphrase": ("⚠️ Wrong passphrase on an approval link", "high"),
    "oauth_register_refused": ("Unknown app tried to connect to ROSE (refused)", "default"),
    "lockdown_on": ("🔒 ROSE lockdown ON", "default"),
}


def _tick_security(state: dict) -> None:
    """Push new security events from today's log (tracked by byte offset)."""
    path = LOG_DIR / f"{datetime.now():%Y-%m-%d}.jsonl"
    if not path.exists():
        return
    key = path.name
    offset = state.setdefault("log_offsets", {}).get(key)
    if offset is None:  # first look at this file: start from the end, don't replay old events
        offset = path.stat().st_size
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read()
        state["log_offsets"] = {key: offset + len(data)}
    for line in data.decode(errors="replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        ev = e.get("event")
        if ev in SECURITY_EVENTS:
            title, prio = SECURITY_EVENTS[ev]
            detail = e.get("tool") or e.get("reason") or e.get("client_id") or ""
            if _cooldown(state, f"sec:{ev}", 120):
                push(title, f"{detail} {(e.get('hits') or [''])[0]}".strip()[:300], priority=prio, tags="warning")


def _tick_system(state: dict) -> None:
    import psutil  # lazy: only the watcher process needs it

    cfg = load_config()
    disk = psutil.disk_usage("/").percent
    if disk >= cfg.get("alert_disk_percent", 90) and _cooldown(state, "disk", 86400):
        push("💾 Disk almost full", f"{disk:.0f}% used on /", priority="high", tags="floppy_disk")
    ram = psutil.virtual_memory().percent
    state["ram_high"] = state.get("ram_high", 0) + 1 if ram >= cfg.get("alert_ram_percent", 92) else 0
    if state["ram_high"] >= 2 and _cooldown(state, "ram", 3600):
        top = sorted(psutil.process_iter(["name", "memory_percent"]), key=lambda p: p.info["memory_percent"] or 0)[-3:]
        push("🧠 RAM almost full", f"{ram:.0f}% used. Top: " + ", ".join(p.info["name"] for p in reversed(top)),
             tags="warning")
    bat = psutil.sensors_battery()
    if bat and not bat.power_plugged and 0 < bat.percent <= cfg.get("alert_battery_percent", 15) \
            and _cooldown(state, "battery", 1800):
        push("🔋 Battery low", f"{bat.percent:.0f}% and unplugged: the laptop (and ROSE) may shut down soon.",
             priority="high", tags="battery")


def _gh_api(path: str):
    """GET a GitHub REST path through the gh CLI (its own login); parsed JSON."""
    from rose.common import run
    out = run(["gh", "api", path], timeout=30)
    return json.loads(out) if out.strip() else None


def _gh_login(state: dict) -> str:
    if not state.get("gh_login"):
        try:
            state["gh_login"] = (_gh_api("user") or {}).get("login", "")
        except Exception:
            return ""
    return state.get("gh_login", "")


REVIEW_RANK = {"CHANGES_REQUESTED": 3, "APPROVED": 2, "COMMENTED": 1}
REVIEW_TITLE = {"CHANGES_REQUESTED": ("🔁 Changes requested", "high", "repeat"),
                "APPROVED": ("✅ PR approved", "default", "white_check_mark"),
                "COMMENTED": ("💬 New review", "default", "speech_balloon")}


def _pr_activity(p: dict, me: str) -> dict:
    """CI status, reviews and review comments by others on one authored PR (fetched in a worker thread)."""
    from rose.tools import github
    out: dict = {"pr": p}
    try:
        out["status"] = github.github_pr_status(p["repo"], p["number"])
    except Exception:
        out["status"] = None
    base = f"repos/{p['repo']}/pulls/{p['number']}"
    for kind, path in (("reviews", f"{base}/reviews?per_page=100"),
                       ("comments", f"{base}/comments?per_page=100&sort=created&direction=desc")):
        try:
            items = _gh_api(path) or []
            out[kind] = [i for i in items if isinstance(i, dict) and (i.get("user") or {}).get("login") != me
                         and (i.get("user") or {}).get("type") != "Bot"]
        except Exception:
            out[kind] = None
    return out


def _review_push(a: dict, seen: dict) -> None:
    """Push new reviews / review comments on one PR (one push per PR per check). Updates `seen` watermarks."""
    p, k = a["pr"], f"{a['pr']['repo']}#{a['pr']['number']}"
    first = k not in seen
    if first and (a.get("reviews") is None or a.get("comments") is None):
        return  # can't seed reliably this time; try again next check
    w = seen.setdefault(k, {"r": 0, "c": 0})
    new_r = [r for r in a.get("reviews") or [] if r.get("id", 0) > w["r"] and r.get("state") in REVIEW_RANK]
    new_c = [c for c in a.get("comments") or [] if c.get("id", 0) > w["c"]]
    if a.get("reviews") is not None:
        w["r"] = max([w["r"]] + [r.get("id", 0) for r in a["reviews"]])
    if a.get("comments") is not None:
        w["c"] = max([w["c"]] + [c.get("id", 0) for c in a["comments"]])
    if first:  # PR seen for the first time: seed silently
        return
    # a plain "commented" review with no body is just the wrapper of its inline comments
    new_r = [r for r in new_r if r["state"] != "COMMENTED" or (r.get("body") or "").strip()]
    if not new_r and not new_c:
        return
    if new_r:
        top = max(new_r, key=lambda r: REVIEW_RANK[r["state"]])
        title, prio, tags = REVIEW_TITLE[top["state"]]
        who = ", ".join(sorted({(r.get("user") or {}).get("login", "?") for r in new_r}))
        msg = f"#{p['number']} {p['title'][:80]} by {who}"
        body = (top.get("body") or "").strip()
        if body:
            msg += f": \"{body[:150]}\""
        url = top.get("html_url") or p.get("url")
    else:
        title, prio, tags = "💬 New review comment" + ("s" if len(new_c) > 1 else ""), "default", "speech_balloon"
        c = min(new_c, key=lambda c: c.get("id", 0))
        msg = f"#{p['number']} {p['title'][:80]}: {(c.get('user') or {}).get('login', '?')}: " \
              f"\"{(c.get('body') or '').strip()[:150]}\""
        url = c.get("html_url") or p.get("url")
    if new_c and new_r:
        msg += f" (+{len(new_c)} inline comment{'s' * (len(new_c) != 1)})"
    elif len(new_c) > 1:
        msg += f" (+{len(new_c) - 1} more)"
    push(title, msg, url=url, priority=prio, tags=tags)


def _tick_github(state: dict) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from rose.tools import github
    try:
        prs = github.github_my_prs()
    except Exception:
        return
    seen = set(state.get("review_seen", []))
    for p in prs["review_requested"]:
        k = f"{p['repo']}#{p['number']}"
        if k not in seen and state.get("github_started"):
            push("👀 Review requested", f"{p['title'][:100]} ({k})", url=p.get("url"), tags="eyes")
        seen.add(k)
    state["review_seen"] = sorted(seen)[-200:]
    failing = set(state.get("ci_failing", []))
    # PRs whose CI was red and hasn't gone green yet (seeded from ci_failing so an upgrade doesn't spam)
    red = set(state.get("ci_red", [k.split(":")[0] for k in failing]))
    now_failing = set()
    me = _gh_login(state)
    authored = prs["authored"][:10]
    with ThreadPoolExecutor(max_workers=4) as pool:
        activity = list(pool.map(lambda p: _pr_activity(p, me), authored))
    for a in activity:
        p, st = a["pr"], a["status"]
        pk = f"{p['repo']}#{p['number']}"
        if st is None:
            continue
        if st["failing"]:
            k = f"{pk}:{','.join(sorted(st['failing']))}"
            now_failing.add(k)
            red.add(pk)
            if k not in failing and state.get("github_started"):
                push("❌ CI failing on your PR", f"#{p['number']} {p['title'][:80]}: {', '.join(st['failing'])[:120]}",
                     url=p.get("url"), priority="high", tags="x")
        elif pk in red and not st["pending"] and st.get("checks_total", 1):
            red.discard(pk)
            if state.get("github_started"):
                push("✅ CI passing again", f"#{p['number']} {p['title'][:80]}", url=p.get("url"),
                     tags="white_check_mark")
    open_keys = {f"{p['repo']}#{p['number']}" for p in authored}
    state["ci_failing"] = sorted(now_failing)
    state["ci_red"] = sorted(red & open_keys)  # merged/closed PRs drop out silently
    if me:
        gh_seen = {k: v for k, v in state.get("pr_activity_seen", {}).items() if k in open_keys}
        for a in activity:
            _review_push(a, gh_seen)
        state["pr_activity_seen"] = gh_seen
    state["github_started"] = True


def _watching() -> list[str]:
    cfg = load_config()
    out = ["schedules", "security events", "disk", "RAM", "battery", "failing CI on your PRs",
           "CI passing again", "reviews and review comments on your PRs", "new review requests", "daily cleanup"]
    if cfg.get("activity_enabled", True):
        out.append(f"work log (active window; summary at {cfg.get('activity_summary_time', '23:00')})")
    if cfg.get("proactive_enabled", True):
        out += ["upcoming meetings (calendar)", "failed CI runs on recently pushed repos", "phone battery",
                f"break reminders (every {cfg.get('break_reminder_minutes', 120)} min of activity)"]
    if cfg.get("failure_report_day", "mon"):
        out.append(f"weekly failure report ({cfg.get('failure_report_day')} {cfg.get('failure_report_time')})")
    try:
        from rose import triggers
        trig = triggers._triggers()
    except Exception:
        trig = []
    if trig:
        out.append("context triggers: " + ", ".join(f"{t['when']} -> {t['routine']}" for t in trig))
    return out


def _tick_phone() -> None:
    """Every 10 s: drop a dead phone link, reconnect, and switch on the Tailscale fallback (phone.keepalive).
    Does nothing until a phone has been connected once."""
    import shutil
    if not shutil.which("adb"):
        return
    from rose.tools import phone
    before = phone._read_last().get("_watch_via")
    st = phone.keepalive()
    if st.get("never_connected"):
        return
    now = time.time()
    if st["via"] != before:
        phone._save_last(_watch_via=st["via"])
        _log({"event": "phone_link", "via": st["via"], "remote_on": st.get("remote_on"),
              "remote_error": st.get("remote_error", "")})
        if st["via"] == "offline":
            _PHONE["offline_since"], _PHONE["told"] = now, False
        elif before == "offline" and _PHONE.get("told"):
            from rose.common import notify_desktop
            notify_desktop("Phone back", f"ROSE reconnected to the phone ({st['via']}).")
    if st["via"] == "offline" and not _PHONE.get("told") and now - _PHONE.get("offline_since", now) > 120:
        _PHONE["told"] = True
        from rose.common import notify_desktop
        notify_desktop("Phone unreachable", st.get("hint") or "ROSE keeps trying every 10 s.")


_PHONE: dict = {}
PHONE_EVERY = 10  # seconds between phone link checks (own thread, so slow watchers don't delay it)


def _phone_loop() -> None:
    while True:
        if load_config().get("watch_enabled", True):
            _safe("phone", _tick_phone)
        time.sleep(PHONE_EVERY)


def _safe(name: str, fn, *args) -> None:
    """Run one watcher; a failure is logged and doesn't stop the others."""
    try:
        fn(*args)
    except Exception as e:
        _log({"event": "watch_error", "watcher": name, "error": f"{type(e).__name__}: {e}"})


def watch_status() -> dict:
    """What ROSE watches for you, when it last checked, and the recent alerts it pushed."""
    st = _load(STATE, {})
    from rose.push import settings
    recent = []
    path = LOG_DIR / f"{datetime.now():%Y-%m-%d}.jsonl"
    if path.exists():
        for line in path.read_text().splitlines()[-400:]:
            if '"push_sent"' in line:
                recent.append(json.loads(line).get("title"))
    return {"running_since": st.get("started"), "last_check": st.get("last_tick"),
            "phone_push": bool(settings()[1]),
            "watching": _watching(),
            "recent_alerts": recent[-10:]}


def _sort_downloads() -> None:
    from rose.tools import organize, undo
    days = float(load_config().get("downloads_autosort_days", 7))
    res = undo.undoable(organize.sort_downloads)(dry_run=False, min_age_days=days)
    if res.get("moved"):
        _log({"event": "downloads_sorted", "moved": res["moved"], "by_folder": res.get("by_folder")})


def run() -> int:
    """Main loop for rose-watch.service."""
    from rose import maintenance
    state = _load(STATE, {})
    state["started"] = datetime.now().isoformat(timespec="seconds")
    state.pop("active_since", None)  # a fresh start (boot/restart) begins a new activity stretch
    state.pop("break_last_tick", None)
    _save(STATE, state)
    last_sys = last_gh = 0.0
    print("rose watch: running", flush=True)
    import threading
    threading.Thread(target=_phone_loop, daemon=True, name="phone-link").start()
    from rose import fingerprint  # phone lock state for fingerprint approvals (idle unless that mode is on)
    threading.Thread(target=fingerprint.lock_loop, daemon=True, name="fp-lock").start()
    from rose.tools import clipboard
    threading.Thread(target=clipboard.history_loop, daemon=True, name="clipboard-history").start()
    while True:
        try:
            if not load_config().get("watch_enabled", True):
                time.sleep(30)
                continue
            _tick_schedules()
            _tick_security(state)
            now = time.time()
            if now - last_sys >= 300:
                last_sys = now
                _tick_system(state)
            if now - last_gh >= 600:
                last_gh = now
                _tick_github(state)
            from rose import activity, triggers
            _safe("activity", activity.tick, state)
            _safe("triggers", triggers.tick, state)
            from rose import failures, proactive
            _safe("proactive", proactive.tick, state)
            if failures.weekly_due(state):
                threading.Thread(target=_safe, args=("failure_report", failures.send_weekly), daemon=True).start()
            if now - state.get("last_cleanup", 0) >= 86400:
                state["last_cleanup"] = now
                maintenance.cleanup()
            if load_config().get("downloads_autosort", False) and now - state.get("last_downloads_sort", 0) >= 86400:
                state["last_downloads_sort"] = now
                _safe("downloads", _sort_downloads)
            state["last_tick"] = datetime.now().isoformat(timespec="seconds")
            _save(STATE, state)
        except Exception as e:  # never die; log and keep going
            _log({"event": "watch_error", "error": f"{type(e).__name__}: {e}"})
        time.sleep(30)
