"""Developer tools for the user's ~/0x projects: git status/diff/commit/today's log, run presets, error context.

Projects are found by spoken name through rose.tools.projects ('' or 'recent' = the folder of VS Code's last
window). Git tools only work inside ~/0x (config projects_root). git_commit never pushes and never skips hooks.

Run presets: project_tasks detects runnable tasks (package.json scripts, pyproject, Makefile, manage.py,
docker compose). project_run only runs a task from that list, in a new terminal, with output tee'd to
~/rose/runs/<project>-<task>.log and the exit code written to <log>.exit. A failed run (not Ctrl-C) sends a
phone push via `python -m rose.tools.dev notify-exit <log>`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from rose.common import ToolError, launch_detached
from rose.config import ROSE_HOME
from rose.tools import projects

RUNS_DIR = ROSE_HOME / "runs"
MAX_FILES = 40
MAX_DIFF = 6000
RECENT_FAIL_SECONDS = 2 * 3600
# Tasks whose names suggest side effects beyond the laptop (deploys, data loss): need confirm=true.
RISKY = re.compile(r"deploy|release|publish|clean|reset|drop|destroy|migrat|push|prune|nuke|rm\b|delete", re.I)


# ---------- projects ----------

def _resolve(project: str) -> Path:
    """Spoken project name -> its folder. '' / 'recent' / 'last' -> VS Code's recent folder."""
    q = (project or "").strip()
    if not q or q.lower() in projects.RECENT:
        r = projects._recent_folder()
        if not r:
            raise ToolError("No recent project found; name the project.")
        return Path(r["path"])
    p = Path(q).expanduser()
    if p.is_absolute() and p.is_dir():
        return p.resolve()
    hits = projects._match(q)
    if not hits:
        raise ToolError(f"No project like '{q}' under {projects._root()}. Use list_projects to see them all.")
    if len(hits) > 1:
        raise ToolError(f"Several projects match '{q}': " + ", ".join(f"{h['name']} ({h['group']})" for h in hits)
                        + ". Say which one.")
    return Path(hits[0]["path"])


def _in_root(path: Path) -> Path:
    root = projects._root()
    if not path.resolve().is_relative_to(root):
        raise ToolError(f"{path} is outside {root}; git tools only work on ~/0x projects.")
    return path


# ---------- git ----------

def _git(path: Path, *args: str, timeout: float = 20, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_PAGER": "cat"}
    try:
        proc = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=timeout,
                              env=env, errors="replace")
    except subprocess.TimeoutExpired:
        raise ToolError(f"git {args[0]} timed out after {timeout}s") from None
    except FileNotFoundError:
        raise ToolError("git is not installed.") from None
    if check and proc.returncode != 0:
        raise ToolError(f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()[:500] or 'no output'}")
    return proc


def _repo(project: str) -> Path:
    path = _in_root(_resolve(project))
    proc = _git(path, "rev-parse", "--show-toplevel", check=False)
    if proc.returncode != 0:
        raise ToolError(f"{path.name} is not a git repository.")
    return Path(proc.stdout.strip())


def git_status(project: str = "") -> dict:
    """Git status of a ~/0x project ('' = recent VS Code folder): branch, ahead/behind, staged/unstaged/
    untracked counts and changed files (first 40)."""
    repo = _repo(project)
    lines = _git(repo, "status", "--porcelain=v1", "-b").stdout.splitlines()
    head = lines[0][3:] if lines and lines[0].startswith("## ") else ""
    branch, _, rest = head.partition("...")
    upstream = rest.split(" [")[0] if rest else ""
    ahead = int(m.group(1)) if (m := re.search(r"ahead (\d+)", head)) else 0
    behind = int(m.group(1)) if (m := re.search(r"behind (\d+)", head)) else 0
    staged = unstaged = untracked = 0
    files = []
    for ln in lines[1:]:
        xy, name = ln[:2], ln[3:]
        if xy == "??":
            untracked += 1
        else:
            staged += xy[0] not in " ?"
            unstaged += xy[1] not in " ?"
        files.append(f"{xy} {name}")
    out = {"project": repo.name, "path": str(repo), "branch": branch.split(" [")[0] or "(detached)",
           "upstream": upstream or None, "ahead": ahead, "behind": behind, "staged": staged,
           "unstaged": unstaged, "untracked": untracked, "clean": not files, "files": files[:MAX_FILES]}
    if len(files) > MAX_FILES:
        out["more_files"] = len(files) - MAX_FILES
    return out


def git_diff_summary(project: str = "", staged: bool = False) -> dict:
    """What changed in a ~/0x project: `git diff --stat` plus the diff (trimmed to ~6 KB) to summarize.
    staged=true: the staged changes (what would be committed)."""
    repo = _repo(project)
    base = ["diff", "--no-color", "--no-ext-diff"] + (["--cached"] if staged else [])
    stat = _git(repo, *base, "--stat").stdout.strip()
    diff = _git(repo, *base, "-U2").stdout
    untracked = [f for f in _git(repo, "ls-files", "--others", "--exclude-standard").stdout.splitlines() if f]
    out = {"project": repo.name, "staged": staged, "stat": stat or "(no changes)",
           "diff": diff[:MAX_DIFF] + (f"\n…(trimmed, {len(diff)} chars total)" if len(diff) > MAX_DIFF else "")}
    if untracked and not staged:
        out["untracked"] = untracked[:MAX_FILES]
    return out


def git_commit(project: str, message: str, add_all: bool = False, confirm: bool = False) -> dict:
    """Commit in a ~/0x project (add_all: `git add -A` first). Needs confirm=true; ask the user first. Never
    pushes, never skips hooks. Refuses an empty message or nothing staged."""
    if not message.strip():
        raise ToolError("Commit message is empty.")
    repo = _repo(project)
    if not confirm:
        raise ToolError(f"Committing in {repo.name} needs confirm=true. Show the user the message and changes "
                        "(git_diff_summary) and ask first.")
    if add_all:
        _git(repo, "add", "-A")
    if _git(repo, "diff", "--cached", "--quiet", check=False).returncode == 0:
        raise ToolError(f"Nothing to commit in {repo.name}: the working tree is clean." if add_all
                        else f"Nothing staged in {repo.name}; use add_all=true to stage everything.")
    proc = _git(repo, "commit", "-m", message.strip(), timeout=180, check=False)
    if proc.returncode != 0:
        raise ToolError(f"git commit failed (hooks?): {(proc.stderr or proc.stdout).strip()[-1500:]}")
    sha = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
    stat = _git(repo, "show", "--stat", "--format=", "HEAD").stdout.strip().splitlines()
    return {"project": repo.name, "commit": sha, "message": message.strip(), "summary": stat[-1] if stat else "",
            "pushed": False}


def _identity(repo: Path) -> set[str]:
    ids = set()
    for key in ("user.email", "user.name"):
        v = _git(repo, "config", key, check=False).stdout.strip().lower()
        if v:
            ids.add(v)
    return ids


def _today_in(repo: Path) -> list[dict]:
    try:
        ids = _identity(repo)
        proc = _git(repo, "log", "--all", "--no-merges", "--since=midnight", "--date=format:%H:%M",
                    "--format=%h%x09%ae%x09%an%x09%ad%x09%s", timeout=15, check=False)
    except ToolError:
        return []
    out = []
    for ln in proc.stdout.splitlines():
        parts = ln.split("\t", 4)
        if len(parts) == 5 and (parts[1].lower() in ids or parts[2].lower() in ids):
            out.append({"commit": parts[0], "time": parts[3], "message": parts[4]})
    return out


def git_log_today(project: str = "") -> dict:
    """The user's commits today (all branches). project: one ~/0x project; '' = every git repo under ~/0x."""
    if project.strip():
        repos = [_repo(project)]
    else:
        repos = [Path(p["path"]) for p in projects._index() if (Path(p["path"]) / ".git").exists()]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_today_in, repos))
    by_repo = {r.name: c for r, c in zip(repos, results, strict=True) if c}
    return {"date": datetime.now().strftime("%Y-%m-%d"), "repos_checked": len(repos),
            "total": sum(len(c) for c in by_repo.values()), "commits": by_repo}


# ---------- run presets ----------

def _node_runner(d: Path) -> str:
    if (d / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (d / "yarn.lock").exists():
        return "yarn"
    if (d / "bun.lockb").exists() or (d / "bun.lock").exists():
        return "bun run"
    return "npm run"


def _make_targets(text: str) -> list[str]:
    out = []
    for m in re.finditer(r"^([A-Za-z0-9][\w.-]*)\s*:(?![=:])", text, re.M):
        t = m.group(1)
        if not t.startswith(".") and "%" not in t and t not in out:
            out.append(t)
    return out


def _tasks(d: Path) -> dict[str, str]:
    tasks: dict[str, str] = {}

    def add(name: str, cmd: str, source: str) -> None:
        tasks[f"{source}:{name}" if name in tasks else name] = cmd

    pkg = d / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text()).get("scripts") or {}
        except (OSError, ValueError):
            scripts = {}
        runner = _node_runner(d)
        for name in scripts:
            add(name, f"{runner} {name}", "npm")
    uv = (d / "uv.lock").exists() or bool(shutil.which("uv"))
    py = "uv run" if uv else ""
    pyp = d / "pyproject.toml"
    if pyp.is_file():
        try:
            data = tomllib.loads(pyp.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        if (d / "tests").is_dir() or (d / "test").is_dir():
            add("test", "uv run pytest" if uv else "python -m pytest", "py")
        for name in (data.get("project", {}).get("scripts") or {}):
            add(name, f"{py} {name}".strip(), "py")
    elif (d / "tests").is_dir() and ((d / "requirements.txt").exists() or (d / "pytest.ini").exists()):
        add("test", "python -m pytest", "py")
    if (d / "manage.py").is_file():
        add("runserver", f"{'uv run ' if pyp.is_file() and uv else ''}python manage.py runserver".strip(), "django")
    for mk in ("Makefile", "makefile", "GNUmakefile"):
        if (d / mk).is_file():
            try:
                for t in _make_targets((d / mk).read_text(errors="replace")):
                    add(t, f"make {t}", "make")
            except OSError:
                pass
            break
    for comp in ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml"):
        if (d / comp).is_file():
            add("compose-up", "docker compose up", "docker")
            break
    return tasks


def project_tasks(project: str = "") -> dict:
    """Runnable tasks of a ~/0x project ('' = recent): package.json scripts, pytest, pyproject scripts, Makefile
    targets, manage.py runserver, docker compose. Returns name -> command for project_run."""
    d = _resolve(project)
    tasks = _tasks(d)
    return {"project": d.name, "path": str(d), "tasks": tasks,
            **({} if tasks else {"note": "No runnable tasks detected."})}


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", s).strip("-")[:60] or "x"


def _log_path(d: Path, task: str) -> Path:
    return RUNS_DIR / f"{_slug(d.name)}-{_slug(task)}.log"


# Runs inside the terminal: $1 dir, $2 command, $3 log, $4 python. Ctrl-C stops the task but not the wrapper
# (trap + tee -i), so the exit code is still recorded. 130 (Ctrl-C) is not treated as a failure.
WRAPPER = r'''cd "$1" || exit 1
rm -f "$3.exit"
trap 'true' INT
printf '\033]0;rose: %s\007' "$2"
echo "\$ $2    (log: $3)"
bash -lc "$2" 2>&1 | tee -i "$3"
code=${PIPESTATUS[0]}
echo "$code" > "$3.exit"
if [ "$code" -ne 0 ] && [ "$code" -ne 130 ]; then "$4" -m rose.tools.dev notify-exit "$3" >/dev/null 2>&1; fi
echo; echo "[exited with $code] press Enter to close"; read -r _
'''


def _terminal_cmd(d: Path, inner: list[str]) -> list[str]:
    exe = shutil.which("ghostty") or shutil.which("gnome-terminal") or shutil.which("x-terminal-emulator")
    if not exe:
        raise ToolError("No terminal found.")
    name = Path(exe).name
    if name == "ghostty":
        return [exe, f"--working-directory={d}", "-e", *inner]
    if name == "gnome-terminal":
        return [exe, f"--working-directory={d}", "--", *inner]
    return [exe, "-e", *inner]


def project_run(project: str, task: str, confirm: bool = False) -> dict:
    """Run a detected task (project_tasks) of a ~/0x project in a new terminal; output logged, phone push if it
    fails. Only listed tasks; deploy/clean/migrate-like ones need confirm=true."""
    d = _resolve(project)
    tasks = _tasks(d)
    if task not in tasks:
        raise ToolError(f"No task '{task}' in {d.name}. Tasks: {', '.join(tasks) or 'none detected'}.")
    cmd = tasks[task]
    if RISKY.search(task) and not confirm:
        raise ToolError(f"Task '{task}' ({cmd}) may change things outside the editor; ask the user, then "
                        "call again with confirm=true.")
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    log = _log_path(d, task)
    for old in (log, Path(f"{log}.exit")):
        old.unlink(missing_ok=True)
    log.touch()
    Path(f"{log}.meta.json").write_text(json.dumps({"project": d.name, "path": str(d), "task": task,
                                                    "command": cmd, "started": time.time()}))
    inner = ["bash", "-c", WRAPPER, "rose-run", str(d), cmd, str(log), sys.executable]
    launch_detached(_terminal_cmd(d, inner), cwd=str(d), name=f"run-{d.name}-{task}")
    return {"project": d.name, "task": task, "command": cmd, "log": str(log),
            "note": "Running in a new terminal; check with project_logs."}


def _runs() -> list[dict]:
    out = []
    for meta in RUNS_DIR.glob("*.log.meta.json"):
        try:
            m = json.loads(meta.read_text())
        except (OSError, ValueError):
            continue
        log = Path(str(meta)[: -len(".meta.json")])
        ex = Path(f"{log}.exit")
        m["log"] = log
        m["exit"] = int(ex.read_text().strip() or -1) if ex.exists() else None
        m["mtime"] = max(log.stat().st_mtime if log.exists() else 0, ex.stat().st_mtime if ex.exists() else 0)
        out.append(m)
    return sorted(out, key=lambda r: -r["mtime"])


def _tail(path: Path, lines: int) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07", "", text).replace("\r\n", "\n")
    return "\n".join(text.splitlines()[-lines:])


def project_logs(project: str, task: str = "", lines: int = 80) -> dict:
    """Tail of the latest project_run log of a ~/0x project (task: a specific one), with its exit code once it
    has finished."""
    d = _resolve(project)
    runs = [r for r in _runs() if r.get("path") == str(d) and (not task or r.get("task") == task)]
    if not runs:
        raise ToolError(f"No runs logged for {d.name}{f' task {task}' if task else ''}; start one with project_run.")
    r = runs[0]
    lines = max(1, min(int(lines), 500))
    return {"project": d.name, "task": r["task"], "command": r["command"],
            "started": datetime.fromtimestamp(r["started"]).strftime("%Y-%m-%d %H:%M"),
            "status": "running (or terminal closed)" if r["exit"] is None else
            ("ok" if r["exit"] == 0 else f"failed (exit {r['exit']})"), "log": str(r["log"]),
            "tail": _tail(r["log"], lines)}


# ---------- explain this error ----------

REF_PATTERNS = (
    re.compile(r'File "([^"]+)", line (\d+)'),                                   # Python tracebacks
    re.compile(r"\bat .*?\(?((?:/|\.{1,2}/|[\w@-]+/)[^\s():]+\.\w{1,5}):(\d+)(?::\d+)?\)?"),  # JS stacks
    re.compile(r"((?:[\w.@-]+/)*[\w.@-]+\.(?:py|js|jsx|ts|tsx|mjs|cjs|go|rs|c|h|cc|cpp|hpp|java|kt|rb|php|"
               r"cs|swift|vue|svelte|css|scss|html|json|toml|yaml|yml|sh)):(\d+)(?::\d+)?"),  # compilers, linters
)


def _refs(text: str, base: Path | None = None) -> list[str]:
    seen: list[str] = []
    for pat in REF_PATTERNS:
        for m in pat.finditer(text):
            ref = f"{m.group(1)}:{m.group(2)}"
            if "site-packages" in ref or "node_modules" in ref or "<frozen" in ref or ref in seen:
                continue
            if base is not None and not Path(m.group(1)).is_absolute() and (base / m.group(1)).exists():
                ref = f"{base / m.group(1)}:{m.group(2)}"
            if ref not in seen:
                seen.append(ref)
    return seen[:15]


def _screen_text() -> str:
    from rose.tools import screenshot

    img = screenshot._grab_x11()
    source = img if img is not None else screenshot.take_screenshot()
    rect, _label = screenshot.resolve_area("active_window")
    return screenshot.screen_text(source, rect=rect, max_chars=3500)


def explain_error(project: str = "") -> dict:
    """Context for 'explain this error': the latest failed project_run log (last 2 h) or else OCR of the active
    window, plus file:line refs. Explain it, or pass it to hand_to_claude to fix."""
    d = _resolve(project) if project.strip() else None
    cutoff = time.time() - RECENT_FAIL_SECONDS
    failed = [r for r in _runs() if r["exit"] not in (None, 0, 130) and r["mtime"] >= cutoff
              and (d is None or r.get("path") == str(d))]
    if failed:
        r = failed[0]
        text = _tail(r["log"], 60)[-4000:]
        base = Path(r["path"])
        return {"source": f"run log of {r['project']} task '{r['task']}' ({r['command']}), exit {r['exit']}",
                "project_path": r["path"], "log": str(r["log"]), "text": text, "file_refs": _refs(text, base)}
    try:
        text = _screen_text()
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"No recent failed run and couldn't read the screen: {type(e).__name__}: {e}") from None
    return {"source": "OCR of the active window (no recent failed run" + (f" for {d.name}" if d else "") + ")",
            **({"project_path": str(d)} if d else {}), "text": text, "file_refs": _refs(text, d)}


# ---------- `python -m rose.tools.dev notify-exit <log>` (called by the run wrapper) ----------

def _notify_exit(log: Path) -> int:
    import threading

    from rose import push

    try:
        meta = json.loads(Path(f"{log}.meta.json").read_text())
    except (OSError, ValueError):
        meta = {"project": log.stem, "task": ""}
    try:
        code = Path(f"{log}.exit").read_text().strip()
    except OSError:
        code = "?"
    title = f"{meta.get('project')} {meta.get('task')}: failed (exit {code})".strip()
    # Short alert only, never log contents (push topics may be readable by others).
    push.push(title, "Ask Rose to explain_error for details.", priority="high", tags="warning")
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.daemon:
            t.join(15)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "notify-exit":
        return _notify_exit(Path(argv[1]))
    print("usage: python -m rose.tools.dev notify-exit <log>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
