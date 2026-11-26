"""Daily work log: what you worked on, sampled from the active window by rose-watch every tick.

Rows go to ~/rose/activity/YYYY-MM-DD.jsonl as {ts, app, project, title}: written when the active window
changes, or every ~5 min as a heartbeat. Nothing is written while you're idle (> 5 min, X screensaver idle
time or xprintidle) or the session is locked; going idle writes an {ts, idle: true} marker (backdated to
when input stopped) so the time in between isn't counted. Time per row = gap to the next row, capped.

Tools: activity_today (minutes per project/app) and standup (commits + Claude Code sessions + time).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path

from rose.common import ToolError, _log, display_env
from rose.config import ROSE_HOME, load_config

ACTIVITY_DIR = ROSE_HOME / "activity"
CLAUDE_EVENTS = ROSE_HOME / "claude_events.jsonl"
IDLE_AFTER = 300  # seconds without input = idle
HEARTBEAT = 300  # write an unchanged sample at least this often
ROW_CAP = HEARTBEAT + 60  # a row never counts for longer than this
TITLE_MAX = 80
TERMINALS = {"gnome-terminal-server", "kitty", "alacritty", "konsole", "xterm", "tilix", "terminator",
             "org.wezfurlong.wezterm", "xfce4-terminal", "urxvt", "st", "foot", "ptyxis", "kgx"}

_last: dict = {}  # last written sample: {"key": (app, project, title), "at": epoch, "idle": bool}
_cache: dict = {}


# ---------------- idle / lock ----------------

def idle_seconds() -> float | None:
    """Seconds since the last keyboard/mouse input (X screensaver extension, else xprintidle), or None."""
    try:
        from Xlib import display
        d = display.Display(display_env()["DISPLAY"])
        try:
            if d.has_extension("MIT-SCREEN-SAVER"):
                info = d.screen().root.screensaver_query_info()
                if info.state == 1:  # ScreenSaverOn: blanked, treat as fully idle
                    return float(max(info.idle / 1000, IDLE_AFTER + 1))
                return info.idle / 1000
        finally:
            d.close()
    except Exception:
        pass
    if shutil.which("xprintidle"):
        try:
            out = subprocess.run(["xprintidle"], capture_output=True, text=True, timeout=3, env=display_env())
            return int(out.stdout.strip()) / 1000
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return None


def _session_id() -> str:
    """The user's graphical logind session (cached 10 min)."""
    if time.time() - _cache.get("sid_at", 0) < 600:
        return _cache.get("sid", "")
    sid = os.environ.get("XDG_SESSION_ID", "")
    if shutil.which("loginctl"):
        try:
            out = subprocess.run(["loginctl", "show-user", str(os.getuid()), "-p", "Display", "--value"],
                                 capture_output=True, text=True, timeout=3)
            sid = out.stdout.strip() or sid
        except (OSError, subprocess.SubprocessError):
            pass
    _cache.update(sid=sid, sid_at=time.time())
    return sid


def is_locked() -> bool:
    """True if logind says the graphical session is locked (False if unknown)."""
    sid = _session_id()
    if not sid or not shutil.which("loginctl"):
        return False
    try:
        out = subprocess.run(["loginctl", "show-session", sid, "-p", "LockedHint", "--value"],
                             capture_output=True, text=True, timeout=3)
        return out.stdout.strip() == "yes"
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------- project detection ----------------

def _projects() -> list[dict]:
    """Project index under projects_root, cached 10 min (never raises)."""
    if time.time() - _cache.get("proj_at", 0) > 600:
        try:
            from rose.tools.projects import _index
            idx = _index()
        except Exception:
            idx = []
        _cache.update(proj=idx, proj_at=time.time())
    return _cache.get("proj", [])


def _project_by_path(path: str) -> str:
    path = os.path.expanduser(path).rstrip("/")
    best = ""
    for p in _projects():
        if (path == p["path"] or path.startswith(p["path"] + "/")) and len(p["path"]) > len(best):
            best = p["path"]
    return os.path.basename(best)


def vscode_folder(title: str) -> str:
    """'● main.py - rose-MCP - Visual Studio Code' -> 'rose-MCP' ('' if not a VS Code/Cursor title)."""
    m = re.search(r" - (Visual Studio Code|Cursor|VSCodium)(?: - Insiders)?$", title)
    if not m:
        return ""
    parts = title[:m.start()].split(" - ")
    folder = parts[-1].strip()
    folder = re.sub(r"\s*\[(SSH|WSL|Dev Container)[^\]]*\]$", "", folder)
    folder = re.sub(r"\s*\(Workspace\)$", "", folder).lstrip("● ").strip()
    return folder


def _terminal_cwd(pid: int | None) -> str:
    """cwd of the newest shell under a terminal window's process."""
    if not pid:
        return ""
    try:
        import psutil
        kids = psutil.Process(pid).children(recursive=True)
        kids.sort(key=lambda p: p.create_time())
        for k in reversed(kids):
            try:
                return k.cwd()
            except (psutil.Error, OSError):
                continue
    except Exception:
        pass
    return ""


def project_of(win: dict) -> str:
    """Best guess of the ~/0x project a window is about ('' if none)."""
    title = win.get("title") or ""
    folder = vscode_folder(title)
    if folder:
        return folder
    m = re.search(r"(~|/home/[^/\s]+)(/[\w.\-/]+)", title)
    if m:
        hit = _project_by_path((os.path.expanduser("~") if m.group(1) == "~" else m.group(1)) + m.group(2))
        if hit:
            return hit
    low = title.lower()
    names = sorted({p["name"] for p in _projects() if len(p["name"]) >= 4}, key=len, reverse=True)
    for n in names:
        if re.search(r"(?<![\w-])" + re.escape(n.lower()) + r"(?![\w-])", low):
            return n
    if (win.get("app") or "").lower() in TERMINALS:
        cwd = _terminal_cwd(win.get("pid"))
        if cwd:
            return _project_by_path(cwd)
    return ""


# ---------------- sampler ----------------

def _append(row: dict, day: Date | None = None) -> None:
    ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
    day = day or datetime.fromisoformat(row["ts"]).date()
    with (ACTIVITY_DIR / f"{day:%Y-%m-%d}.jsonl").open("a") as f:
        f.write(json.dumps(row) + "\n")


def sample(now: float | None = None) -> dict | None:
    """One sampler step (rose-watch calls it every tick). Returns the row written, if any."""
    now = now or time.time()
    idle = idle_seconds()
    if (idle is not None and idle >= IDLE_AFTER) or is_locked():
        if _last and not _last.get("idle"):
            since = now - (idle if idle is not None and idle < 86400 else 0)
            row = {"ts": datetime.fromtimestamp(max(since, _last.get("at", 0))).isoformat(timespec="seconds"),
                   "idle": True}
            _append(row)
            _last.update(idle=True, at=now, key=None)
            return row
        return None
    from rose.tools.apps import active_window
    win = active_window()
    if not win:
        return None
    title = (win.get("title") or "")[:TITLE_MAX]
    key = (win.get("app") or "", project_of(win), title)
    if key == _last.get("key") and now - _last.get("at", 0) < HEARTBEAT:
        return None
    row = {"ts": datetime.fromtimestamp(now).isoformat(timespec="seconds"), "app": key[0], "project": key[1],
           "title": key[2]}
    _append(row)
    _last.update(key=key, at=now, idle=False)
    return row


# ---------------- reading ----------------

def _parse_day(day: str) -> Date:
    day = (day or "").strip().lower()
    if day in ("", "today"):
        return Date.today()
    if day == "yesterday":
        return Date.today() - timedelta(days=1)
    try:
        return Date.fromisoformat(day)
    except ValueError:
        raise ToolError('date must be YYYY-MM-DD, "today" or "yesterday".') from None


def _rows(day: Date) -> list[dict]:
    path = ACTIVITY_DIR / f"{day:%Y-%m-%d}.jsonl"
    out = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            r = json.loads(line)
            r["_t"] = datetime.fromisoformat(r["ts"]).timestamp()
            out.append(r)
        except (ValueError, KeyError, TypeError):
            continue
    out.sort(key=lambda r: r["_t"])
    return out


def _fmt_min(m: float) -> str:
    m = int(round(m))
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def summarize(day: Date) -> dict:
    rows = _rows(day)
    by_project: dict[str, float] = {}
    by_app: dict[str, float] = {}
    active = 0.0
    first = last = None
    end_of_day = datetime.combine(day + timedelta(days=1), datetime.min.time()).timestamp()
    for i, r in enumerate(rows):
        if r.get("idle"):
            continue
        nxt = rows[i + 1]["_t"] if i + 1 < len(rows) else min(time.time(), end_of_day, r["_t"] + 60)
        dur = max(0.0, min(nxt - r["_t"], ROW_CAP))
        if dur <= 0:
            continue
        active += dur
        proj = r.get("project") or "(no project)"
        by_project[proj] = by_project.get(proj, 0) + dur
        by_app[r.get("app") or "?"] = by_app.get(r.get("app") or "?", 0) + dur
        first = first if first is not None else r["_t"]
        last = r["_t"] + dur

    def top(d: dict) -> dict:
        return {k: round(v / 60) for k, v in sorted(d.items(), key=lambda kv: -kv[1]) if v >= 60}

    return {"date": day.isoformat(), "active_minutes": round(active / 60),
            "first_activity": datetime.fromtimestamp(first).strftime("%H:%M") if first else None,
            "last_activity": datetime.fromtimestamp(last).strftime("%H:%M") if last else None,
            "by_project": top(by_project), "by_app": top(by_app)}


def activity_today(date: str = "") -> dict:
    """Work log for a day (default today; YYYY-MM-DD or "yesterday"): active minutes per project and per app,
    first and last activity (24-hour). From the active-window sampler in rose-watch."""
    day = _parse_day(date)
    s = summarize(day)
    if not s["active_minutes"]:
        s["note"] = ("No activity recorded for that day." if load_config().get("activity_enabled", True)
                     else "Activity logging is off (config activity_enabled).")
    return s


# ---------------- standup ----------------

def _git_email() -> str:
    email = load_config().get("git_author_email", "")
    if email:
        return email
    try:
        return subprocess.run(["git", "config", "--global", "user.email"], capture_output=True, text=True,
                              timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _repos() -> list[dict]:
    return [p for p in _projects() if (Path(p["path"]) / ".git").exists()]


def commits(day: Date, email: str) -> dict[str, list[str]]:
    """{project: [commit subjects]} by `email` on `day`, across git repos under projects_root."""
    if not email:
        return {}
    since, until = f"{day:%Y-%m-%d} 00:00", f"{day + timedelta(days=1):%Y-%m-%d} 00:00"

    def one(p: dict) -> tuple[str, list[str]]:
        try:
            out = subprocess.run(["git", "-C", p["path"], "-c", "core.fsmonitor=false", "log", "--all", "--no-merges", f"--author={email}",
                                  f"--since={since}", f"--until={until}", "--format=%H%x09%s"],
                                 capture_output=True, text=True, timeout=8)
        except (OSError, subprocess.SubprocessError):
            return p["name"], []
        seen, subjects = set(), []
        for line in out.stdout.splitlines():
            h, _, s = line.partition("\t")
            if h and h not in seen:
                seen.add(h)
                subjects.append(s.strip())
        return p["name"], subjects

    with ThreadPoolExecutor(max_workers=8) as pool:
        return {name: subs for name, subs in pool.map(one, _repos()) if subs}


def _event_time(e: dict) -> datetime | None:
    ts = e.get("ts")
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts)
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt
    except (TypeError, ValueError, OSError):
        return None


def claude_sessions(day: Date) -> list[dict]:
    """Claude Code sessions on `day` from ~/rose/claude_events.jsonl: [{project, prompts, first}] (if present)."""
    try:
        lines = CLAUDE_EVENTS.read_text(errors="replace").splitlines()
    except OSError:
        return []
    sessions: dict[str, dict] = {}
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = _event_time(e) if isinstance(e, dict) else None
        if not t or t.date() != day:
            continue
        sid = str(e.get("session_id") or e.get("cwd") or "?")
        s = sessions.setdefault(sid, {"project": e.get("project") or os.path.basename(e.get("cwd") or "") or "?",
                                      "prompts": 0, "first": t.strftime("%H:%M"), "summary": ""})
        if e.get("event") == "UserPromptSubmit":
            s["prompts"] += 1
        if not s["summary"] and (e.get("summary") or e.get("message")):
            s["summary"] = str(e.get("summary") or e.get("message"))[:80]
    return list(sessions.values())


def _day_lines(day: Date, email: str) -> list[str]:
    lines = []
    for proj, subs in sorted(commits(day, email).items(), key=lambda kv: -len(kv[1])):
        shown = "; ".join(s[:60] for s in subs[:3]) + (f" (+{len(subs) - 3} more)" if len(subs) > 3 else "")
        lines.append(f"- {proj}: {len(subs)} commit{'s' * (len(subs) != 1)}: {shown}")
    sess = claude_sessions(day)
    if sess:
        per: dict[str, int] = {}
        for s in sess:
            per[s["project"]] = per.get(s["project"], 0) + 1
        lines.append(f"- Claude Code: {len(sess)} session{'s' * (len(sess) != 1)} ("
                     + ", ".join(f"{p} x{n}" if n > 1 else p for p, n in per.items()) + ")")
    s = summarize(day)
    if s["active_minutes"]:
        top = ", ".join(f"{p} {_fmt_min(m)}" for p, m in list(s["by_project"].items())[:4])
        lines.append(f"- Time: {_fmt_min(s['active_minutes'])} active ({s['first_activity']}-{s['last_activity']})"
                     + (f"; {top}" if top else ""))
    return lines or ["- Nothing recorded."]


def standup(date: str = "") -> str:
    """Short standup for a day (default today): yesterday's and today's commits across ~/0x repos, Claude Code
    sessions and time per project, plus CI failing on your PRs (as blockers)."""
    day = _parse_day(date)
    email = _git_email()
    prev = day - timedelta(days=1)
    if prev.weekday() == 6 and not _rows(prev):  # Monday: "yesterday" means Friday if the weekend was empty
        prev -= timedelta(days=2)
    out = [f"Standup {day:%a %d %b}", f"Yesterday ({prev:%a %d %b}):", *_day_lines(prev, email),
           f"Today ({day:%a %d %b}):", *_day_lines(day, email)]
    try:
        from rose.watch import STATE, _load
        failing = _load(STATE, {}).get("ci_failing", [])
    except Exception:
        failing = []
    out.append("Blockers: " + (", ".join("CI failing " + k.split(":")[0] for k in failing[:5]) or "none noted"))
    if not email:
        out.append("(No git email: set config git_author_email to include commits.)")
    return "\n".join(out)


# ---------------- daily summary push + cleanup (from the watch loop) ----------------

def summary_text(day: Date) -> str:
    s = summarize(day)
    parts = [f"{_fmt_min(s['active_minutes'])} active ({s['first_activity']}-{s['last_activity']})"]
    if s["by_project"]:
        parts.append("Projects: " + ", ".join(f"{p} {_fmt_min(m)}" for p, m in list(s["by_project"].items())[:5]))
    if s["by_app"]:
        parts.append("Apps: " + ", ".join(f"{a} {_fmt_min(m)}" for a, m in list(s["by_app"].items())[:4]))
    n = sum(len(v) for v in commits(day, _git_email()).values())
    if n:
        parts.append(f"{n} commit{'s' * (n != 1)}")
    return "\n".join(parts)


def tick(state: dict, now: datetime | None = None) -> None:
    """Watch-loop hook: sample, push the daily summary once at activity_summary_time, prune old logs daily."""
    cfg = load_config()
    if not cfg.get("activity_enabled", True):
        return
    now = now or datetime.now()
    try:
        sample()
    except Exception as e:
        _log({"event": "activity_sample_failed", "error": f"{type(e).__name__}: {e}"})
    at = str(cfg.get("activity_summary_time", "23:00"))
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", at.strip())
    if m:
        due = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        today = now.date().isoformat()
        if due <= now < due + timedelta(hours=2) and state.get("activity_summary_day") != today:
            state["activity_summary_day"] = today
            if summarize(now.date())["active_minutes"]:
                from rose.push import push
                push("📊 Daily work log", summary_text(now.date()), tags="bar_chart")
    if now.timestamp() - state.get("activity_pruned", 0) >= 86400:
        state["activity_pruned"] = now.timestamp()
        keep = int(cfg.get("activity_keep_days", 90))
        cutoff = (now - timedelta(days=keep)).strftime("%Y-%m-%d")
        for p in ACTIVITY_DIR.glob("*.jsonl") if ACTIVITY_DIR.is_dir() else []:
            if p.stem < cutoff:
                p.unlink(missing_ok=True)
