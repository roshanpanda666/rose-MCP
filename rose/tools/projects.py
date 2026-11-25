"""Projects: the user's code lives under ~/0x (config `projects_root`). Rose finds a project by its spoken name
("rose phone app", "pcn hub", "rms backend") and opens it, so the user never has to give a path.

Layout: ~/0x/work/* = work (office) projects, ~/0x/projects/* = personal projects, plus other folders in ~/0x.
Every folder one or two levels down is a candidate; deeper ones only when they look like a project root
(.git, package.json, pyproject.toml...). The index is rebuilt on every call, so new projects show up at once.
"""

from __future__ import annotations

import difflib
import json
import re
import shutil
from pathlib import Path
from typing import Literal
from urllib.parse import unquote

from rose.common import ToolError, launch_detached
from rose.config import load_config

SKIP = {"node_modules", "venv", ".venv", "env", "__pycache__", "dist", "build", "target", ".git", ".next", "logs"}
MARKERS = (".git", "package.json", "pyproject.toml", "requirements.txt", "go.mod", "Cargo.toml", "pom.xml",
           "build.gradle", "composer.json", "Gemfile")
MAX_DEPTH = 3


def _root() -> Path:
    return Path(load_config().get("projects_root") or "~/0x").expanduser().resolve()


def _is_project(p: Path) -> bool:
    return any((p / m).exists() for m in MARKERS)


def _index() -> list[dict]:
    root = _root()
    if not root.is_dir():
        raise ToolError(f"Projects folder {root} doesn't exist (config projects_root).")
    out: list[dict] = []

    def walk(d: Path, depth: int) -> None:
        try:
            kids = sorted(c for c in d.iterdir() if c.is_dir() and not c.name.startswith(".") and c.name not in SKIP)
        except OSError:
            return
        for c in kids:
            container = depth == 1 and c.name in ("work", "projects")  # holds projects, isn't one
            if not container and (depth <= 2 or _is_project(c)):
                rel = c.relative_to(root)
                group = "work" if rel.parts[0] == "work" else "personal" if rel.parts[0] == "projects" else "other"
                out.append({"name": c.name, "path": str(c), "group": group, "depth": depth})
            if depth < MAX_DEPTH and not (depth >= 2 and _is_project(c)):
                walk(c, depth + 1)

    walk(root, 1)
    return out


FILLER = re.compile(r"\b(the|my|our|a|project|projects|folder|repo|repository|codebase|code ?base|app folder)\b", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _clean(query: str) -> str:
    """'the rose project' -> 'rose' (spoken filler words aren't part of the folder name)."""
    q = FILLER.sub(" ", query).strip()
    return q or query


def _score(query: str, p: dict) -> float:
    q, n = _norm(_clean(query)), _norm(p["name"])
    rel = _norm(p["path"].split("/0x/", 1)[-1]) if "/0x/" in p["path"] else n
    if not q:
        return 0.0
    if q == n:
        s = 1.0
    elif q == rel:
        s = 0.99
    elif n.startswith(q) or q.startswith(n):
        # the closer in length wins: "rose" -> rose-MCP over rose-phone-app
        s = 0.8 + 0.15 * min(len(q), len(n)) / max(len(q), len(n)) if min(len(q), len(n)) >= 3 else 0.0
    elif q in n or q in rel:
        s = 0.8
    else:
        s = difflib.SequenceMatcher(None, q, n).ratio() * 0.85
    return s - 0.01 * (p["depth"] - 1)  # a shallower folder wins a tie ("rms" over "rms/rms")


def _match(name: str) -> list[dict]:
    ranked = sorted(((round(_score(name, p), 3), p) for p in _index()), key=lambda x: -x[0])
    ranked = [(s, p) for s, p in ranked if s >= 0.55]
    if not ranked:
        return []
    best = ranked[0][0]
    return [p for s, p in ranked if best - s < 0.02]


def list_projects(query: str = "", group: Literal["all", "work", "personal", "other"] = "all") -> list[dict]:
    """The user's projects under ~/0x (work = ~/0x/work, personal = ~/0x/projects). query filters by name."""
    items = _index()
    if group != "all":
        items = [p for p in items if p["group"] == group]
    if query.strip():
        items = [p for p in items if _score(query, p) >= 0.55]
    return [{k: p[k] for k in ("name", "path", "group")} for p in items]


def _find_file(project: Path, file: str) -> Path:
    direct = (project / file).resolve()
    if direct.is_file() and direct.is_relative_to(project):
        return direct
    want = Path(file).name.lower()
    hits = []
    for f in project.rglob("*"):
        if any(part in SKIP or part.startswith(".") for part in f.relative_to(project).parts[:-1]):
            continue
        if f.is_file() and f.name.lower() == want:
            hits.append(f)
            if len(hits) > 8:
                break
    if not hits:
        raise ToolError(f"No file '{file}' in {project}.")
    if len(hits) > 1:
        raise ToolError("Several files match:\n" + "\n".join(str(h.relative_to(project)) for h in hits)
                        + "\nCall again with file= one of these paths.")
    return hits[0]


VSCODE_STATE = Path("~/.config/Code/User/globalStorage/storage.json").expanduser()
RECENT = {"recent", "last", "latest", "current", "previous", "last project", "recent project"}


def _recent_folder() -> dict | None:
    """The folder of VS Code's last active window, else the most recently changed project in ~/0x."""
    try:
        ws = json.loads(VSCODE_STATE.read_text()).get("windowsState", {})
        for w in [ws.get("lastActiveWindow") or {}, *(ws.get("openedWindows") or [])]:
            uri = w.get("folder") or ""
            if uri.startswith("file://"):
                d = Path(unquote(uri[7:]))
                if d.is_dir():
                    return {"name": d.name, "path": str(d), "group": "recent", "depth": 1}
    except (OSError, ValueError, AttributeError):
        pass
    items = _index()
    return max(items, key=lambda p: Path(p["path"]).stat().st_mtime, default=None)


def open_terminal(folder: Path) -> None:
    """The user's terminal (x-terminal-emulator: ghostty) in that folder."""
    exe = shutil.which("ghostty") or shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal")
    if not exe:
        raise ToolError("No terminal found.")
    cmd = [exe, f"--working-directory={folder}"] if Path(exe).name in ("ghostty", "gnome-terminal") else [exe]
    launch_detached(cmd, cwd=str(folder), name="terminal")


def open_project(name: str, file: str = "", app: Literal["code", "files"] = "code", terminal: bool = False,
                 pick: int = 0) -> str:
    """Open a ~/0x project by name (fuzzy: 'rose', 'rms backend'; 'recent' = last VS Code folder) in VS Code
    (app=files: file manager). file: also open it. terminal: also a terminal there. Ambiguous: pick=N."""
    if name.strip().lower() in RECENT:
        r = _recent_folder()
        if not r:
            raise ToolError("No recent project found.")
        hits = [r]
    else:
        hits = _match(name)
    if not hits:
        raise ToolError(f"No project like '{name}' under {_root()}. Use list_projects to see them all.")
    if len(hits) > 1 and not pick:
        lines = "\n".join(f"{i}. {p['name']} ({p['group']}): {p['path']}" for i, p in enumerate(hits, 1))
        raise ToolError(f"Several projects match '{name}':\n{lines}\nCall again with pick=N.")
    if pick and not 1 <= pick <= len(hits):
        raise ToolError(f"pick must be 1..{len(hits)}.")
    p = hits[(pick or 1) - 1]
    target = _find_file(Path(p["path"]), file) if file.strip() else None
    if app == "code":
        open_in_code(Path(p["path"]), target)
    else:
        launch_detached(["xdg-open", str(target or p["path"])], name="files")
    if terminal:
        open_terminal(Path(p["path"]))
    what = f"{target.relative_to(p['path'])} in {p['name']}" if target else p["name"]
    return (f"Opened {what} ({target or p['path']}) in {'VS Code' if app == 'code' else 'the file manager'}"
            + (" and a terminal there." if terminal else "."))


def in_projects(path: Path) -> bool:
    """True for anything under ~/0x: those open in VS Code."""
    try:
        return path.resolve().is_relative_to(_root())
    except (OSError, ToolError):
        return False


def open_in_code(folder: Path, file: Path | None = None) -> None:
    """VS Code with the folder as workspace (reuses its window if already open) and the file in a tab."""
    exe = shutil.which("code")
    if not exe:
        raise ToolError("VS Code ('code') isn't installed; use app='files'.")
    cmd = [exe, str(folder)] + ([str(file)] if file else [])
    launch_detached(cmd, name="code")
