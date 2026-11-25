"""File tools, restricted to the allowlisted directories (allowed_dirs in config.toml)."""

from __future__ import annotations

import fnmatch
import os
import shutil
from datetime import datetime
from pathlib import Path

from rose.common import ToolError
from rose.config import allowed_dirs, load_config

MAX_RESULTS = 200


def _resolve(path: str) -> Path:
    """Resolve `path` (following symlinks) and refuse anything outside the allowlist."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        # Relative paths are taken relative to the first allowlisted dir (the ROSE workspace).
        p = allowed_dirs()[0] / p
    p = p.resolve()
    roots = allowed_dirs()
    if not any(p == root or p.is_relative_to(root) for root in roots):
        raise ToolError(
            f"Access denied: {p} is outside the allowed folders: "
            + ", ".join(str(r) for r in roots)
        )
    return p


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def list_allowed_dirs() -> list[str]:
    """Folders the file tools may use."""
    return [str(d) for d in allowed_dirs()]


def list_dir(path: str, show_hidden: bool = False) -> str:
    """List a directory: type, size, mtime, name."""
    p = _resolve(path)
    if not p.is_dir():
        raise ToolError(f"Not a directory: {p}")
    lines = []
    for entry in sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
        if not show_hidden and entry.name.startswith("."):
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        kind = "dir " if entry.is_dir() else "file"
        size = "" if entry.is_dir() else _fmt_size(st.st_size)
        mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        lines.append(f"{kind}  {size:>8}  {mtime}  {entry.name}")
    return f"{p}\n" + ("\n".join(lines) if lines else "(empty)")


def read_file(path: str, offset: int = 0) -> str:
    """Read a UTF-8 text file. offset = start char (for paging past the output cap)."""
    p = _resolve(path)
    if not p.is_file():
        raise ToolError(f"Not a file: {p}")
    limit = load_config()["max_read_bytes"]
    size = p.stat().st_size
    if size > limit:
        raise ToolError(f"{p} is {_fmt_size(size)}, over the {_fmt_size(limit)} read limit.")
    try:
        text = p.read_text()
    except UnicodeDecodeError:
        raise ToolError(f"{p} is not a UTF-8 text file.") from None
    return text[max(0, offset):] if offset else text


def write_file(path: str, content: str, overwrite: bool = False, confirm: bool = False) -> str:
    """Write a text file. Replacing one needs overwrite=true and confirm=true."""
    p = _resolve(path)
    if p.is_dir():
        raise ToolError(f"{p} is a directory.")
    if p.exists() and not (overwrite and confirm):
        raise ToolError(
            f"{p} already exists. To replace it, call again with overwrite=true and confirm=true."
        )
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"Wrote {len(content)} characters to {p}"


def create_folder(path: str) -> str:
    """Create a folder (and any missing parents). Fine if it already exists."""
    p = _resolve(path)
    if p.exists() and not p.is_dir():
        raise ToolError(f"{p} exists and is a file, not a folder.")
    if p.is_dir():
        return f"Folder already exists: {p}"
    p.mkdir(parents=True)
    return f"Created folder {p}"


def move_file(source: str, destination: str, confirm: bool = False) -> str:
    """Move/rename a file or folder. Replacing a file needs confirm=true."""
    src = _resolve(source)
    dst = _resolve(destination)
    if not src.exists():
        raise ToolError(f"Source does not exist: {src}")
    if dst.is_dir():
        dst = dst / src.name
        _resolve(str(dst))
    if dst.exists():
        if not confirm:
            raise ToolError(f"{dst} already exists. To replace it, call again with confirm=true.")
        if dst.is_dir():
            raise ToolError(f"{dst} is an existing folder; ROSE won't replace folders.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"Moved {src} -> {dst}"


def search_files(
    root: str, name_pattern: str = "*", contains: str = "", max_results: int = 50
) -> str:
    """Find files under root by name glob, optionally containing text."""
    base = _resolve(root)
    if not base.is_dir():
        raise ToolError(f"Not a directory: {base}")
    max_results = max(1, min(max_results, MAX_RESULTS))
    limit = load_config()["max_read_bytes"]
    needle = contains.lower()
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "node_modules"]
        for name in filenames:
            if not fnmatch.fnmatch(name.lower(), name_pattern.lower()):
                continue
            fp = Path(dirpath) / name
            if needle:
                try:
                    if fp.stat().st_size > limit or needle not in fp.read_text(errors="ignore").lower():
                        continue
                except OSError:
                    continue
            hits.append(str(fp))
            if len(hits) >= max_results:
                return "\n".join(hits) + f"\n(stopped at {max_results} results)"
    return "\n".join(hits) if hits else "No matching files."
