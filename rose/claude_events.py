"""Claude Code reports back to ROSE through hooks (and runs ROSE's background Claude jobs).

Claude Code runs `python -m rose.claude_events <Event>` (or `rose claude-hook <Event>`) with the hook JSON
on stdin for UserPromptSubmit, Notification and Stop. Each call:

* appends one line to ~/rose/claude_events.jsonl:
  {"ts": epoch, "event", "session_id", "cwd", "project", "summary", "message"}
* writes ~/rose/claude_state.json: {"state": "busy"|"waiting"|"done", "project", "message", "session_id", "ts"}
  (the phone app, desktop avatar and activity log read these two files: keep the format)
* pushes to the phone: Stop -> "Claude finished", Notification (permission / question) -> "Claude needs you".

The hook must never slow down or break Claude Code: only rose.config is imported up front (no mcp), the
files are written synchronously, the push happens in a detached child, stdout stays empty and the exit
code is always 0. Headless/SDK runs (Forge, `claude -p`, ROSE's own background jobs) are ignored, Stop
within ~20 s of the prompt doesn't push, and pushes are rate-limited per session.

`rose claude-hooks install|uninstall|status [--settings PATH]` merges these hooks into ~/.claude/settings.json.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path

from rose.config import ROSE_HOME, load_config

EVENTS_PATH = ROSE_HOME / "claude_events.jsonl"
STATE_PATH = ROSE_HOME / "claude_state.json"
HOOK_STATE_PATH = ROSE_HOME / "claude_hook_state.json"  # private: prompt start times, last pushes
HOOK_EVENTS = ("UserPromptSubmit", "Notification", "Stop")
STATES = {"UserPromptSubmit": "busy", "Notification": "waiting", "Stop": "done"}
# Notification types that don't need the user (idle reminders after Stop, auth ok): log only.
QUIET_NOTIFICATIONS = {"idle_prompt", "auth_success"}
TAIL_BYTES = 256 * 1024
MAX_STDIN = 2 * 1024 * 1024
EVENTS_MAX_BYTES = 5 * 1024 * 1024  # rotate the jsonl once it gets big (keeps one .1 copy)


def _cfg() -> dict:
    try:
        c = load_config()
    except Exception:
        c = {}
    return {"push": bool(c.get("claude_push_enabled", True)),
            "min_task": float(c.get("claude_push_min_task_s", 20)),
            "gap": float(c.get("claude_push_min_gap_s", 60))}


def headless(env: dict | None = None) -> bool:
    """True for runs nobody is watching: SDK / `claude -p` (Forge, background jobs)."""
    env = os.environ if env is None else env
    if env.get("ROSE_CLAUDE_JOB") or env.get("ROSE_FORGE_JOB"):
        return True
    if env.get("CLAUDE_CODE_SKIP_PLUGIN_MCP_SERVERS") == "1" and env.get("MCP_CONNECTION_NONBLOCKING") == "false":
        return True  # Forge's launch environment (rose/tools/forge.py)
    return env.get("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk")


def _one_line(text: str, n: int = 200) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def last_assistant_text(transcript_path: str) -> str:
    """Text of the last assistant message in a Claude Code transcript (reads only the file's tail)."""
    try:
        p = Path(transcript_path).expanduser()
        size = p.stat().st_size
        with p.open("rb") as f:
            f.seek(max(0, size - TAIL_BYTES))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except (OSError, TypeError, ValueError):
        return ""
    for line in reversed(lines):
        if '"assistant"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # the first (cut) line, or garbage
        if rec.get("type") != "assistant":
            continue
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            text = "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
            if text.strip():
                return text.strip()
    return ""


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def _append_event(entry: dict) -> None:
    try:
        if EVENTS_PATH.stat().st_size > EVENTS_MAX_BYTES:
            EVENTS_PATH.replace(EVENTS_PATH.with_name(EVENTS_PATH.name + ".1"))
    except OSError:
        pass
    with EVENTS_PATH.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _hook_state(update, now: float) -> dict:
    """Read-modify-write the private hook state under a lock; `update(state)` mutates it. Returns it."""
    import fcntl
    with open(HOOK_STATE_PATH.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            st = json.loads(HOOK_STATE_PATH.read_text())
        except (OSError, ValueError):
            st = {}
        st.setdefault("sessions", {})
        update(st)
        st["sessions"] = {k: v for k, v in st["sessions"].items() if now - v.get("seen", 0) < 2 * 86400}
        _write_json(HOOK_STATE_PATH, st)
        return st


def handle(event: str, payload: dict, env: dict | None = None, now: float | None = None) -> dict | None:
    """Record one hook event. Returns the push to send ({title, message, priority, tags}) or None."""
    if event not in HOOK_EVENTS or headless(env):
        return None
    now = time.time() if now is None else now
    sid = str(payload.get("session_id") or "")
    cwd = str(payload.get("cwd") or "")
    project = Path(cwd).name if cwd else ""
    ntype = str(payload.get("notification_type") or "")
    message = ""
    if event == "UserPromptSubmit":
        summary = _one_line(str(payload.get("prompt") or ""))
    elif event == "Notification":
        message = _one_line(str(payload.get("message") or ""), 300)
        summary = message
    else:
        text = str(payload.get("last_assistant_message") or "") or \
            last_assistant_text(str(payload.get("transcript_path") or ""))
        summary = _one_line(text)
    ROSE_HOME.mkdir(parents=True, exist_ok=True)
    _append_event({"ts": now, "event": event, "session_id": sid, "cwd": cwd, "project": project,
                   "summary": summary, "message": message})

    quiet = event == "Notification" and ntype in QUIET_NOTIFICATIONS
    if not quiet:
        _write_json(STATE_PATH, {"state": STATES[event], "project": project, "message": summary,
                                 "session_id": sid, "ts": now})
    cfg = _cfg()
    decision: dict = {}

    def upd(st):
        s = st["sessions"].setdefault(sid, {})
        s["seen"] = now
        if event == "UserPromptSubmit":
            s["prompt_ts"] = now
            return
        if quiet or not cfg["push"]:
            return
        if event == "Stop" and now - s.get("prompt_ts", 0) < cfg["min_task"]:
            return  # quick answer: the user is probably still looking
        last = s.setdefault("pushed", {})
        if now - last.get(event, 0) < cfg["gap"] or now - st.get("last_push", 0) < 5:
            return
        last[event] = now
        st["last_push"] = now
        decision["push"] = True

    _hook_state(upd, now)
    if not decision:
        return None
    where = project or "Claude Code"
    if event == "Stop":
        return {"title": f"✅ Claude finished in {where}", "message": summary or "Done.",
                "priority": "default", "tags": "white_check_mark"}
    return {"title": _one_line(f"⏳ Claude needs you in {where}: {message or 'waiting for you'}", 200),
            "message": message or "Claude Code is waiting for you.", "priority": "high", "tags": "hourglass"}


def _push_detached(p: dict) -> None:
    """Send the push from a detached child so the hook returns at once (push() uses a thread + httpx)."""
    try:
        if os.fork():
            return
    except OSError:
        return
    try:
        os.setsid()
        fd = os.open(os.devnull, os.O_RDWR)
        for i in (0, 1, 2):
            os.dup2(fd, i)  # don't hold Claude Code's stdout/stderr pipes open
        import threading

        from rose.push import push
        push(p["title"], p["message"], priority=p["priority"], tags=p["tags"])
        for t in threading.enumerate():
            if t is not threading.main_thread():
                t.join(15)
    finally:
        os._exit(0)


def hook_main(event: str, stdin=None) -> int:
    """Entry point for the hook command. Never fails, never writes stdout."""
    try:
        raw = (stdin or sys.stdin).read(MAX_STDIN)
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        event = event or str(payload.get("hook_event_name") or "")
        p = handle(event, payload)
        if p:
            _push_detached(p)
    except Exception as e:  # noqa: BLE001 - a hook must never break Claude Code
        try:
            sys.stderr.write(f"rose claude-hook: {type(e).__name__}: {e}\n")
        except Exception:
            pass
    return 0


# ---------------- settings.json installer ----------------

DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"
MARKERS = ("rose.claude_events", "rose claude-hook")


def hook_command(event: str) -> str:
    """Absolute command for one hook. Uses the installed rose's Python directly (`-m rose.claude_events`,
    ~0.1 s) instead of the `rose` script, whose CLI imports the MCP server stack (~0.7 s per prompt)."""
    rose = shutil.which("rose") or str(Path.home() / ".local" / "bin" / "rose")
    try:
        with open(rose, "rb") as f:
            first = f.readline().decode(errors="replace").strip()
    except OSError:
        first = ""
    if first.startswith("#!") and "python" in first and " " not in first[2:].strip():
        py = first[2:].strip()
        if os.path.isabs(py) and os.access(py, os.X_OK):
            return f"{shlex.quote(py)} -m rose.claude_events {event}"
    return f"{shlex.quote(rose)} claude-hook {event}"


def _ours(h: dict) -> bool:
    return isinstance(h, dict) and any(m in str(h.get("command", "")) for m in MARKERS)


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text()
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def _strip(settings: dict) -> dict:
    """Settings with every ROSE hook removed (others untouched; emptied groups/events dropped)."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for ev in list(hooks):
        groups = hooks[ev]
        if not isinstance(groups, list):
            continue
        kept = []
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                inner = [h for h in g["hooks"] if not _ours(h)]
                if not inner and any(_ours(h) for h in g["hooks"]):
                    continue
                g = {**g, "hooks": inner}
            kept.append(g)
        if kept:
            hooks[ev] = kept
        else:
            del hooks[ev]
    if not hooks:
        settings.pop("hooks", None)
    return settings


def _save(path: Path, data: dict) -> str:
    backup = ""
    if path.exists():
        backup = str(path.with_name(f"{path.name}.rose-backup-{time.strftime('%Y%m%d-%H%M%S')}"))
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".rose-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)
    return backup


def install(path: Path = DEFAULT_SETTINGS) -> str:
    settings = _load(path)
    before = json.dumps(settings, sort_keys=True)
    settings = _strip(settings)
    hooks = settings.setdefault("hooks", {})
    for ev in HOOK_EVENTS:
        hooks.setdefault(ev, []).append({"hooks": [{"type": "command", "command": hook_command(ev), "timeout": 10}]})
    if json.dumps(settings, sort_keys=True) == before:
        return f"Already installed in {path}."
    backup = _save(path, settings)
    return f"Installed ROSE hooks ({', '.join(HOOK_EVENTS)}) in {path}." + (f" Backup: {backup}" if backup else "")


def uninstall(path: Path = DEFAULT_SETTINGS) -> str:
    settings = _load(path)
    before = json.dumps(settings, sort_keys=True)
    settings = _strip(settings)
    if json.dumps(settings, sort_keys=True) == before:
        return f"No ROSE hooks in {path}."
    backup = _save(path, settings)
    return f"Removed ROSE hooks from {path}. Backup: {backup}"


def status(path: Path = DEFAULT_SETTINGS) -> str:
    settings = _load(path)
    found = {ev: [h.get("command") for g in (settings.get("hooks") or {}).get(ev, []) if isinstance(g, dict)
                  for h in g.get("hooks", []) if _ours(h)] for ev in HOOK_EVENTS}
    lines = [f"{path}"]
    for ev, cmds in found.items():
        lines.append(f"  {ev:<17} {'installed: ' + cmds[0] if cmds else 'not installed'}")
    try:
        st = json.loads(STATE_PATH.read_text())
        lines.append(f"state: {st.get('state')} in {st.get('project')} at {time.strftime('%H:%M:%S', time.localtime(st['ts']))}")
    except (OSError, ValueError, KeyError):
        lines.append("state: no events yet")
    return "\n".join(lines)


def hooks_cli(action: str, settings_path: str = "") -> int:
    path = Path(settings_path).expanduser() if settings_path else DEFAULT_SETTINGS
    try:
        print({"install": install, "uninstall": uninstall, "status": status}[action](path))
    except (OSError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


# ---------------- background job runner ----------------

def run_job(job_dir: str) -> int:
    """Run one background Claude job (started by hand_to_claude(mode='background')), then push the result.

    Runs detached in its own systemd scope, so a ROSE restart doesn't kill it. meta.json holds the command."""
    import subprocess
    d = Path(job_dir)
    meta_path = d / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta.update(status="running", runner_pid=os.getpid())
    _write_json(meta_path, meta)
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    env["ROSE_CLAUDE_JOB"] = meta["id"]  # our hooks ignore this run; we push the result ourselves
    try:
        with open(d / "output.json", "w") as out, open(d / "stderr.log", "w") as err:
            proc = subprocess.run(meta["cmd"], cwd=meta["cwd"], stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                  env=env, timeout=meta["timeout"] + 30)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = 124
    except OSError as e:
        rc = 127
        meta["error"] = str(e)
    result, cost, denied, is_error = "", None, [], rc != 0
    try:
        out = json.loads((d / "output.json").read_text())
        result = str(out.get("result", ""))
        cost = out.get("total_cost_usd")
        is_error = bool(out.get("is_error")) or (rc != 0 and not result)
        denied = sorted({p.get("tool_name", "?") for p in out.get("permission_denials") or []})
        meta["session_id"] = out.get("session_id")
    except (OSError, ValueError):
        if rc == 124:
            meta["error"] = f"timed out after {meta['timeout']} s"
        elif not meta.get("error"):
            meta["error"] = ((d / "stderr.log").read_text(errors="replace")[-500:].strip()
                             or f"claude exited {rc} with no output")
    meta.update(status="failed" if is_error or meta.get("error") else "done", finished=time.time(), exit_code=rc,
                result=result[:15000], cost_usd=cost, denied_tools=denied)
    _write_json(meta_path, meta)
    try:
        from rose.push import push
        project = Path(meta["cwd"]).name
        if meta["status"] == "done":
            push(f"✅ Claude job done in {project}", _one_line(result) or "Done.", tags="robot")
        else:
            push(f"❌ Claude job failed in {project}", _one_line(meta.get("error") or result) or "Failed.",
                 priority="high", tags="x")
        import threading
        for t in threading.enumerate():
            if t is not threading.main_thread():
                t.join(15)
    except Exception:
        pass
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "--job":
        return run_job(argv[1])
    return hook_main(argv[0] if argv else "")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
