"""Hand a task to Claude Code inside VS Code: no clicking or typing directions needed.

How: the Claude Code extension's URI handler (vscode://anthropic.claude-code/open?prompt=...) opens a new Claude
tab with the prompt already in its input box. ROSE puts the right project's VS Code window in front, sends that
URI with `code --open-url`, waits for the new tab, then before each Enter re-checks (and if needed refocuses) that
window by its X id, clicks into the Claude prompt box (found by OCR of the task text), and presses Enter.

Safety: Enter is pressed only once the window title shows the new tab (the title changed after the URI); else the
prompt stays typed in but unsent, and we say so. Needs confirm=true: Claude Code can edit files and run commands.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sys
import time
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from rose.common import ToolError, launch_detached, run
from rose.config import ROSE_HOME, load_config
from rose.tools import projects

URI = "vscode://anthropic.claude-code/open?prompt="
MAX_TASK = 6000
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
SESSION_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

JOBS_DIR = ROSE_HOME / "claude_jobs"
JOB_ID_RE = re.compile(r"^[a-z0-9]{10}$")
# Background jobs: edits inside the project are auto-accepted; Bash only for read-only git and tests.
# Anything else needing permission is denied (headless runs can't ask). No web tools, no MCP servers.
JOB_TOOLS = "Read,Edit,Write,Glob,Grep,Bash"
JOB_ALLOWED = ["Read", "Edit", "Write", "Glob", "Grep",
               *(f"Bash({c}:*)" for c in ("git status", "git diff", "git log", "git show", "git branch",
                                          "uv run pytest", "pytest", "python -m pytest", "npm test", "npm run test",
                                          "pnpm test", "yarn test", "cargo test", "go test"))]
JOB_DENY = ["WebFetch", "WebSearch", "mcp__rose", *(f"Bash({c}:*)" for c in (
    "git push", "git commit", "git reset", "git checkout", "git clean", "rm", "curl", "wget", "ssh", "scp", "sudo"))]
JOB_SYSTEM = ("You are Claude Code running headless in the background, started by ROSE from the user's phone. "
              "Nobody can answer questions or approve anything: do what you can with the allowed tools and say "
              "clearly what is left. You can edit files in this project and run read-only git and test commands; "
              "never commit, push or delete. Your final answer is read on a phone: lead with the outcome, be brief.")


def _vscode_windows() -> list[dict]:
    from rose.tools.apps import list_windows
    return [w for w in list_windows() if w.get("title", "").endswith("Visual Studio Code")]


def _folder_of(title: str) -> str:
    """'<tab> - rose-MCP - Visual Studio Code' -> 'rose-MCP'; 'rose-MCP - Visual Studio Code' (no tab open) too."""
    parts = title.rsplit(" - ", 2)
    if len(parts) < 2 or parts[-1] != "Visual Studio Code":
        return ""
    return parts[-2].strip()


def _active_title() -> str:
    from rose.tools.apps import active_window
    return (active_window() or {}).get("title", "")


def _active_id() -> str:
    from rose.tools.apps import active_window
    return (active_window() or {}).get("id", "")


def _same_id(a: str, b: str) -> bool:
    try:
        return int(a, 16) == int(b, 16)
    except (TypeError, ValueError):
        return False


def _wait(pred, seconds: float, step: float = 0.15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def _window_for(folder: Path) -> dict:  # noqa: C901
    """VS Code's window on that folder: focused, opened first if needed."""
    from rose.tools.apps import focus_window
    name = folder.name
    win = next((w for w in _vscode_windows() if _folder_of(w["title"]) == name), None)
    if not win:
        projects.open_in_code(folder)
        win = _wait(lambda: next((w for w in _vscode_windows() if _folder_of(w["title"]) == name), None), 20, 0.4)
        if not win:
            raise ToolError(f"VS Code didn't open a window on {folder} in 20 s.")
        time.sleep(2)  # let the extensions (Claude Code) activate in the fresh window
    focus_window(str(win["id"]))
    if not _wait(lambda: _folder_of(_active_title()) == name, 2):
        raise ToolError(f"Couldn't bring VS Code's '{name}' window to the front. Nothing was sent.")
    return win


def _resolve_folder(project: str) -> Path:
    """The project folder: named ~/0x project, else the VS Code window in front, else the recent project."""
    if project.strip():
        if project.strip().lower() in projects.RECENT:
            r = projects._recent_folder()
            hits = [r] if r else []
        else:
            hits = projects._match(project)
        if not hits:
            raise ToolError(f"No project like '{project}' under ~/0x. Use list_projects.")
        if len(hits) > 1:
            raise ToolError("Several projects match: " + ", ".join(p["name"] for p in hits) + ". Name one exactly.")
        return Path(hits[0]["path"])
    front = _folder_of(_active_title())
    wins = _vscode_windows()
    win = next((w for w in wins if _folder_of(w["title"]) == front), None) if front else None
    if win is None and wins:
        win = wins[0]
    if win is not None:
        match = [p for p in projects._index() if p["name"] == _folder_of(win["title"])]
        if match:
            return Path(match[0]["path"])
    r = projects._recent_folder()
    if not r:
        raise ToolError("No VS Code window or recent project. Say which project.")
    return Path(r["path"])


def session_dir(folder: Path) -> Path:
    """~/.claude/projects/<encoded folder>: Claude Code replaces every non-alphanumeric char with '-'."""
    return CLAUDE_PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", str(Path(folder).expanduser().absolute()))


def latest_session(folder: Path) -> str:
    """Newest Claude Code session id for that folder (by transcript mtime), or ''."""
    d = session_dir(folder)
    try:
        files = [f for f in d.glob("*.jsonl") if SESSION_RE.match(f.stem)]
    except OSError:
        return ""
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return files[0].stem if files else ""


def hand_to_claude(task: str, project: str = "", confirm: bool = False, continue_session: bool = False,
                   mode: Literal["vscode", "background"] = "vscode") -> str:
    """Give a task to Claude Code (VS Code tab, sent). project: ~/0x name (default: front VS Code window/recent).
    continue_session: newest session there. mode=background: headless, edits+tests only; claude_job_status.
    Needs confirm."""
    task = task.strip()
    if not task:
        raise ToolError("Give the task text.")
    if len(task) > MAX_TASK:
        raise ToolError(f"Task too long ({len(task)} chars, max {MAX_TASK}). Shorten it.")
    if mode not in ("vscode", "background"):
        raise ToolError("mode must be 'vscode' or 'background'.")
    if not confirm:
        raise ToolError("This sends a task to Claude Code, which can edit files and run commands. "
                        "If the user asked for it, call again with confirm=true.")
    if mode == "background":
        folder = _resolve_folder(project)
        if continue_session:
            raise ToolError("continue_session works only with mode='vscode'.")
        return _start_job(task, folder)
    exe = shutil.which("code")
    if not exe:
        raise ToolError("VS Code ('code') isn't installed.")
    folder = _resolve_folder(project)

    uri, note = URI + quote(task, safe=""), ""
    if continue_session:
        sid = latest_session(folder)
        if sid:
            uri += "&session=" + sid
            note = f" Continued session {sid[:8]}."
        else:
            note = " No earlier Claude session for this folder, so this is a new session."

    win_id = _window_for(folder).get("id", "")
    before, started = _active_title(), time.time()
    launch_detached([exe, "--open-url", uri], name="code-open-url")
    # the new Claude tab becomes the active editor: the window title changes, same folder
    after = _wait(lambda: (t := _active_title()) != before and _folder_of(t) == folder.name and t, 10)
    if not after:
        return (f"Opened a Claude Code tab in VS Code ({folder.name}) with the task typed in, but couldn't confirm "
                "the tab is in front, so I did NOT press Enter. Check VS Code and press Enter there." + note)
    sent = _press_enter_until_submitted(folder, task, started, win_id=win_id)
    if sent is None:
        return ("Couldn't keep VS Code's window in front before sending; the task is typed in Claude Code but NOT "
                "sent." + note)
    if not sent:
        return (f"Opened a Claude Code tab in VS Code ({folder.name}) with the task typed in and pressed Enter, but "
                "couldn't confirm Claude received it. Check VS Code and press Enter there if needed." + note)
    return (f"Sent to Claude Code in VS Code ({folder.name}, {folder}).{note} Approvals it asks for show in VS Code "
            "(use live screen from the phone).")


def _prompt_submitted(folder: Path, task: str, since: float) -> bool | None:
    """True once ROSE's Claude Code UserPromptSubmit hook logged this task for that folder after `since`.
    None when the hooks aren't installed (no events file), so we can't tell."""
    from rose.claude_events import EVENTS_PATH
    try:
        with open(EVENTS_PATH, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    root, head = str(folder), " ".join(task.split())[:40]
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("ts", 0) < since:
            break
        if e.get("event") == "UserPromptSubmit" and (e.get("cwd") or "").startswith(root) \
                and " ".join(str(e.get("message") or e.get("summary") or "").split()).startswith(head):
            return True
    return False




def _focus_target(folder: Path, win_id: str = "") -> bool:
    """Make sure that VS Code window (by X id when known, and by folder in its title) is the active one; refocus it
    if something else took focus. False if it can't be brought back."""
    def ok() -> bool:
        if win_id and not _same_id(_active_id(), win_id):
            return False
        return _folder_of(_active_title()) == folder.name
    if ok():
        return True
    if not win_id:
        return False
    from rose.tools.apps import focus_window
    try:
        focus_window(win_id)
    except ToolError:
        return False
    return bool(_wait(ok, 2))


def _focus_claude_input(task: str) -> bool:
    """Click into the Claude Code prompt box of the new tab, so the Enter that follows lands there.

    Keystrokes alone can't do it: the extension's /open URI opens the tab with preserveFocus, and after ROSE raises
    the window synthetic keys (Enter, even Ctrl+Shift+P) don't reach VS Code until a real click focuses it. The box
    already shows the task, so OCR its first words in the active window and click the best, lowest match (the box
    sits at the bottom of the tab). False (nothing clicked) when the text can't be found: then no Enter is sent,
    since it could land in an open file instead."""
    from rose.tools.pointer import _norm, _ocr_candidates
    words = _norm(" ".join(task.split()[:4]))
    if not words:
        return False
    try:
        cands, _ = _ocr_candidates(words, "active_window")
    except Exception:  # noqa: BLE001  (no tesseract, no screen grab, ...): just don't click
        return False
    if not cands:
        return False
    best = max(c["score"] for c in cands)
    c = max((c for c in cands if c["score"] >= best - 0.05), key=lambda c: c["y"])
    run(["xdotool", "mousemove", str(c["x"]), str(c["y"]), "click", "1"])
    time.sleep(0.3)
    return True


def _press_enter_until_submitted(folder: Path, task: str, since: float, tries: int = 9,
                                 win_id: str = "") -> bool | None:
    """The webview fills and focuses its input a variable moment after the tab shows, so one early Enter can land
    nowhere. Before every Enter: bring the right VS Code window back to the front and click into the Claude input box.
    Press Enter, wait for the submit hook, retry. A repeat Enter after a successful send hits an empty
    input and does nothing. True: sent (confirmed by the hook, or hooks absent and Enter pressed);
    False: pressed but never confirmed; None: the window couldn't be focused before any press."""
    time.sleep(0.5)  # the loop below re-checks (OCR) until the box shows the task, so no long blind wait
    for i in range(tries):
        if not _focus_target(folder, win_id):
            return None if i == 0 else False
        if not _focus_claude_input(task):  # prompt box not on screen yet: look again, never Enter blind
            time.sleep(0.5)
            continue
        if not _focus_target(folder, win_id):  # the click must not have moved us to another window
            return None if i == 0 else False
        run(["xdotool", "key", "--clearmodifiers", "Return"])
        if _wait(lambda: _prompt_submitted(folder, task, since) is not False, 2.5, 0.25):
            return True
        if _prompt_submitted(folder, task, since) is None:  # no hooks: can't confirm; a few spaced presses
            if i >= 2:
                return True
    return False


# ---------------- background jobs ----------------

def _job_cfg() -> dict:
    c = load_config()
    return {"timeout": int(c.get("claude_job_timeout", 1800)), "model": str(c.get("claude_job_model", "")),
            "max_running": int(c.get("claude_job_max_running", 2))}


def _read_meta(d: Path) -> dict:
    return json.loads((d / "meta.json").read_text())


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    try:
        return os.waitpid(int(pid), os.WNOHANG) == (0, 0)
    except ChildProcessError:
        return True  # not our child (started before a ROSE restart) but running


def _job_view(d: Path) -> dict:
    m = _read_meta(d)
    pid = m.get("runner_pid") or m.get("pid")
    if m["status"] in ("starting", "running") and not _pid_alive(pid) and time.time() - m["started"] > 15:
        m["status"], m["error"] = "failed", m.get("error") or "the job runner stopped (reboot or killed)"
    return m


def _start_job(task: str, folder: Path) -> str:
    claude = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    if not os.path.exists(claude):
        raise ToolError("Claude Code (`claude`) is not installed on this machine.")
    if not folder.is_dir():
        raise ToolError(f"{folder} isn't a folder.")
    cfg = _job_cfg()
    running = 0
    for d in JOBS_DIR.glob("*/") if JOBS_DIR.exists() else []:
        try:
            running += _job_view(d)["status"] in ("starting", "running")
        except (OSError, ValueError, KeyError):
            continue
    if running >= cfg["max_running"]:
        raise ToolError(f"{running} background Claude jobs are already running. Check claude_job_status.")
    job_id = secrets.token_hex(5)
    d = JOBS_DIR / job_id
    d.mkdir(parents=True)
    prompt = " " + task if task.startswith("-") else task  # never parsed as a CLI option
    cmd = ["timeout", "--kill-after=15", str(cfg["timeout"]), claude, "-p", prompt, "--output-format", "json",
           "--permission-mode", "acceptEdits", "--tools", JOB_TOOLS, "--strict-mcp-config",
           "--setting-sources", "user", "--allowedTools", *JOB_ALLOWED, "--disallowedTools", *JOB_DENY,
           "--append-system-prompt", JOB_SYSTEM]
    if cfg["model"]:
        cmd += ["--model", cfg["model"]]
    meta = {"id": job_id, "status": "starting", "task": task, "cwd": str(folder), "cmd": cmd,
            "timeout": cfg["timeout"], "started": time.time()}
    (d / "meta.json").write_text(json.dumps(meta, indent=2))
    proc = launch_detached([sys.executable, "-m", "rose.claude_events", "--job", str(d)], cwd=str(folder),
                           name=f"claude-job-{job_id}")
    del proc  # the runner records its own pid (runner_pid) in meta.json
    return (f"Started background Claude job {job_id} in {folder.name} ({folder}). It can edit files there and run "
            f"read-only git and test commands; no web, commit or push. Result goes to the phone; or "
            f"claude_job_status(job_id='{job_id}').")


def claude_job_status(job_id: str = "") -> dict:
    """Background Claude job (hand_to_claude mode=background): status and result. No job_id: recent jobs."""
    if job_id:
        if not JOB_ID_RE.match(job_id):
            raise ToolError("Invalid job id.")
        d = JOBS_DIR / job_id
        if not d.is_dir():
            raise ToolError(f"No background Claude job {job_id}.")
        m = _job_view(d)
        rep = {"job_id": job_id, "status": m["status"], "project": Path(m["cwd"]).name, "task": m["task"][:300],
               "seconds": round((m.get("finished") or time.time()) - m["started"])}
        for k in ("error", "result", "denied_tools", "cost_usd", "session_id"):
            if m.get(k):
                rep[k] = m[k]
        if m["status"] in ("starting", "running"):
            rep["next"] = "Still working; the result is pushed to the phone when done."
        return rep
    rows = []
    for d in JOBS_DIR.glob("*/") if JOBS_DIR.exists() else []:
        try:
            rows.append(_job_view(d))
        except (OSError, ValueError, KeyError):
            continue
    rows.sort(key=lambda m: m["started"], reverse=True)
    return {"jobs": [{"job_id": m["id"], "status": m["status"], "project": Path(m["cwd"]).name,
                      "task": m["task"][:120], "started": time.strftime("%Y-%m-%d %H:%M", time.localtime(m["started"])),
                      "summary": (m.get("error") or m.get("result") or "")[:160]} for m in rows[:10]]}
