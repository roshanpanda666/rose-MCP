"""File tools, restricted to the allowlisted directories (allowed_dirs in config.toml)."""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
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


# ---------------- copy / trash / edit / archives ----------------

TRASH = Path.home() / ".local" / "share" / "Trash"
MAX_UNZIP_BYTES = 4 * 1024**3


def copy_target(source: str, destination: str) -> tuple[Path, Path]:
    """(src, final destination) for copy_file: copying into an existing folder keeps the source's name."""
    src = _resolve(source)
    dst = _resolve(destination)
    if dst.is_dir():
        dst = _resolve(str(dst / src.name))
    return src, dst


def copy_file(source: str, destination: str, confirm: bool = False) -> str:
    """Copy a file or folder. Into an existing folder keeps the name. Replacing a file needs confirm=true."""
    src, dst = copy_target(source, destination)
    if not src.exists():
        raise ToolError(f"Source does not exist: {src}")
    if dst == src or dst.is_relative_to(src):
        raise ToolError(f"Can't copy {src} into itself.")
    if dst.exists():
        if dst.is_dir():
            raise ToolError(f"{dst} is an existing folder; ROSE won't replace folders.")
        if not confirm:
            raise ToolError(f"{dst} already exists. To replace it, call again with confirm=true.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)
    return f"Copied {src} -> {dst}"


def _trash_info(p: Path) -> Path | None:
    """The newest .trashinfo in the home trash whose Path= is `p`."""
    from urllib.parse import unquote

    best = None
    for info in (TRASH / "info").glob("*.trashinfo"):
        try:
            text = info.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("Path=") and Path(unquote(line[5:])) == p:
                if best is None or info.stat().st_mtime > best.stat().st_mtime:
                    best = info
    return best


def trash_file(path: str, confirm: bool = False) -> str:
    """Move a file or folder to the Trash (recoverable; undo_last puts it back). Needs confirm=true."""
    p = _resolve(path)
    if not p.exists() and not p.is_symlink():
        raise ToolError(f"Does not exist: {p}")
    if p in allowed_dirs() or p == Path.home():
        raise ToolError(f"{p} is an allowed root folder; ROSE won't trash it.")
    if not confirm:
        raise ToolError(f"Moving {p} to the Trash needs confirm=true. Ask the user first.")
    proc = subprocess.run(["gio", "trash", str(p)], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise ToolError(f"Couldn't trash {p}: {(proc.stderr or proc.stdout).strip()[:300]}")
    info = _trash_info(p)
    return f"Moved {p} to the Trash" + (f" (as {info.stem})" if info else "")


def edit_file(path: str, find: str = "", replace: str = "", replace_all: bool = False, append: str = "") -> str:
    """Change part of a text file: replace exact text `find` with `replace` (one match unless replace_all), and/or
    add `append` at the end. Undo restores the old file."""
    p = _resolve(path)
    if not p.is_file():
        raise ToolError(f"Not a file: {p}")
    if not find and not append:
        raise ToolError("Give `find` (with `replace`) and/or `append`.")
    if p.stat().st_size > load_config()["max_read_bytes"]:
        raise ToolError(f"{p} is too big to edit here.")
    try:
        text = p.read_text()
    except UnicodeDecodeError:
        raise ToolError(f"{p} is not a UTF-8 text file.") from None
    changes = []
    if find:
        n = text.count(find)
        if n == 0:
            raise ToolError(f"Text not found in {p.name}; read_file it and copy the exact text.")
        if n > 1 and not replace_all:
            raise ToolError(f"The text appears {n} times in {p.name}; add more context or set replace_all=true.")
        text = text.replace(find, replace)
        changes.append(f"replaced {n} match{'es' if n > 1 else ''}")
    if append:
        if text and not text.endswith("\n") and not append.startswith("\n"):
            text += "\n"
        text += append
        changes.append(f"appended {len(append)} characters")
    p.write_text(text)
    return f"Edited {p}: " + " and ".join(changes)


def zip_files(paths: list[str], archive: str) -> str:
    """Zip files/folders into a new .zip archive."""
    import zipfile

    if not paths:
        raise ToolError("Nothing to zip.")
    out = _resolve(archive if archive.lower().endswith(".zip") else archive + ".zip")
    if out.exists():
        raise ToolError(f"{out} already exists; pick another name.")
    srcs = [_resolve(s) for s in paths]
    for s in srcs:
        if not s.exists():
            raise ToolError(f"Does not exist: {s}")
    out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for s in srcs:
                if s.is_dir():
                    for f in sorted(s.rglob("*")):
                        if f.is_file() and not f.is_symlink() and f != out:
                            z.write(f, f.relative_to(s.parent))
                            count += 1
                else:
                    z.write(s, s.name)
                    count += 1
    except Exception:
        out.unlink(missing_ok=True)
        raise
    return f"Created {out} ({count} files, {_fmt_size(out.stat().st_size)})"


def unzip_target(archive: str, destination: str = "") -> tuple[Path, Path]:
    src = _resolve(archive)
    dst = _resolve(destination) if destination else _resolve(str(src.parent / src.stem))
    return src, dst


def unzip_file(archive: str, destination: str = "") -> str:
    """Extract a .zip (default: a folder named after it, next to it). Never overwrites existing files."""
    import zipfile

    src, dst = unzip_target(archive, destination)
    if not src.is_file():
        raise ToolError(f"Not a file: {src}")
    try:
        z = zipfile.ZipFile(src)
    except zipfile.BadZipFile:
        raise ToolError(f"{src.name} is not a valid zip file.") from None
    with z:
        members = [m for m in z.infolist() if not m.is_dir()]
        if sum(m.file_size for m in members) > MAX_UNZIP_BYTES:
            raise ToolError(f"{src.name} would unpack to over {_fmt_size(MAX_UNZIP_BYTES)}; refusing.")
        targets = []
        for m in members:
            t = (dst / m.filename).resolve()
            if not t.is_relative_to(dst):
                raise ToolError(f"{src.name} contains an unsafe path ({m.filename}); refusing.")
            if t.exists():
                raise ToolError(f"{t} already exists; extract somewhere else.")
            targets.append((m, t))
        for m, t in targets:
            t.parent.mkdir(parents=True, exist_ok=True)
            with z.open(m) as fin, open(t, "wb") as fout:
                shutil.copyfileobj(fin, fout)
    dst.mkdir(parents=True, exist_ok=True)
    return f"Extracted {len(targets)} files from {src.name} to {dst}"
