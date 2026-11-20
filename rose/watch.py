"""`rose watch`: the proactive part of ROSE. Runs as rose-watch.service.

Every 30 s: due schedules (reminders, reports, scheduled Forge tasks) and new security events.
Every 5 min: disk, RAM, battery. Every 10 min: GitHub (failing CI on your PRs, new review requests).
Daily: cleanup of old screenshots/logs. Alerts go to the phone (ntfy) and the desktop.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from typing import Literal

import psutil

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


def schedule_add(kind: Literal["remind", "report", "forge"], time_hhmm: str, text: str = "",
                 repeat: Literal["once", "daily", "weekdays", "weekly"] = "once", date: str = "",
                 weekday: str = "", confirm: bool = False) -> dict:
    """Schedule something. kind: remind (push `text` to the phone), report (system + GitHub summary,
    full text on the bridge), forge (run `text` as a Forge task; result on the bridge; it can only read
    and draft). time_hhmm is 24h local time like "18:00"; date "YYYY-MM-DD" for one-offs (default:
    next occurrence); repeat once/daily/weekdays/weekly (+ weekday mon..sun). Scheduled Forge tasks
    need confirm=true."""
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", time_hhmm.strip()):
        raise ToolError("time_hhmm must be 24-hour HH:MM, e.g. 09:00 or 18:30.")
    if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ToolError("date must be YYYY-MM-DD.")
    if repeat == "weekly" and weekday.lower()[:3] not in WEEKDAYS:
        raise ToolError("weekly needs weekday: mon..sun.")
    if kind in ("remind", "forge") and not text.strip():
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
    from rose.tools.bridge import post_as
    try:
        if item["kind"] == "remind":
            push("⏰ Reminder", item["text"], priority="high", tags="alarm_clock")
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


def _tick_github(state: dict) -> None:
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
    now_failing = set()
    for p in prs["authored"][:10]:
        try:
            st = github.github_pr_status(p["repo"], p["number"])
        except Exception:
            continue
        if st["failing"]:
            k = f"{p['repo']}#{p['number']}:{','.join(sorted(st['failing']))}"
            now_failing.add(k)
            if k not in failing and state.get("github_started"):
                push("❌ CI failing on your PR", f"#{p['number']} {p['title'][:80]}: {', '.join(st['failing'])[:120]}",
                     url=p.get("url"), priority="high", tags="x")
    state["ci_failing"] = sorted(now_failing)
    state["github_started"] = True


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
            "watching": ["schedules", "security events", "disk", "RAM", "battery", "failing CI on your PRs",
                         "new review requests", "daily cleanup"],
            "recent_alerts": recent[-10:]}


def run() -> int:
    """Main loop for rose-watch.service."""
    from rose import maintenance
    state = _load(STATE, {})
    state["started"] = datetime.now().isoformat(timespec="seconds")
    _save(STATE, state)
    last_sys = last_gh = 0.0
    print("rose watch: running", flush=True)
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
            if now - state.get("last_cleanup", 0) >= 86400:
                state["last_cleanup"] = now
                maintenance.cleanup()
            state["last_tick"] = datetime.now().isoformat(timespec="seconds")
            _save(STATE, state)
        except Exception as e:  # never die; log and keep going
            _log({"event": "watch_error", "error": f"{type(e).__name__}: {e}"})
        time.sleep(30)
