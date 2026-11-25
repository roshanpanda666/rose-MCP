"""Tidy ~/Downloads: move loose files into type folders (Images, Documents, ...). Undo puts them all back.

On demand with sort_downloads; rose-watch also runs it once a day when config `downloads_autosort` is true
(files older than `downloads_autosort_days`). Only top-level files move: folders, hidden files and unfinished
downloads stay put; a name clash gets " (2)" etc.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from rose.common import ToolError

DOWNLOADS = Path.home() / "Downloads"
CATEGORIES = {
    "Images": {"jpg", "jpeg", "png", "gif", "webp", "svg", "heic", "bmp", "tif", "tiff", "avif", "ico"},
    "Documents": {"pdf", "doc", "docx", "odt", "rtf", "txt", "md", "xls", "xlsx", "ods", "csv", "ppt", "pptx",
                  "odp", "epub", "pages", "numbers", "key"},
    "Archives": {"zip", "tar", "gz", "tgz", "bz2", "xz", "7z", "rar", "zst"},
    "Audio": {"mp3", "wav", "flac", "ogg", "m4a", "aac", "opus"},
    "Video": {"mp4", "mkv", "mov", "webm", "avi", "m4v"},
    "Installers": {"deb", "rpm", "appimage", "flatpakref", "run", "exe", "msi", "dmg", "apk"},
    "Code": {"py", "js", "ts", "json", "yaml", "yml", "toml", "sh", "sql", "html", "css", "ipynb", "xml", "env"},
}
PARTIAL = {"crdownload", "part", "download", "tmp", "partial", "opdownload"}


def _category(f: Path) -> str:
    ext = f.suffix.lower().lstrip(".")
    if f.name.lower().endswith((".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst")):
        return "Archives"
    return next((c for c, exts in CATEGORIES.items() if ext in exts), "Other")


def plan(min_age_days: float = 1, folder: Path | None = None) -> list[tuple[Path, Path]]:
    base = folder or DOWNLOADS
    if not base.is_dir():
        raise ToolError(f"{base} doesn't exist.")
    cutoff = time.time() - min_age_days * 86400
    moves = []
    taken: set[Path] = set()
    for f in sorted(base.iterdir()):
        if not f.is_file() or f.is_symlink() or f.name.startswith("."):
            continue
        if f.suffix.lower().lstrip(".") in PARTIAL:
            continue
        try:
            if f.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        target = base / _category(f) / f.name
        stem, suf, n = target.stem, target.suffix, 2
        while target.exists() or target in taken:
            target = target.with_name(f"{stem} ({n}){suf}")
            n += 1
        taken.add(target)
        moves.append((f, target))
    return moves


def sort_downloads(dry_run: bool = True, min_age_days: float = 1) -> dict:
    """Tidy ~/Downloads into Images/Documents/Archives/Audio/Video/Installers/Code/Other folders. Only loose
    files older than min_age_days. dry_run=true (default) shows the plan; dry_run=false moves them (undo_last
    puts them back)."""
    moves = plan(min_age_days)
    summary: dict[str, int] = {}
    for _, t in moves:
        summary[t.parent.name] = summary.get(t.parent.name, 0) + 1
    if dry_run or not moves:
        return {"dry_run": dry_run, "files": len(moves), "by_folder": summary,
                "sample": [f"{s.name} -> {t.parent.name}/" for s, t in moves[:25]],
                "note": "Nothing to sort." if not moves else "Call again with dry_run=false to move them."}
    done = []
    for s, t in moves:
        try:
            t.parent.mkdir(exist_ok=True)
            if t.exists():
                continue
            shutil.move(str(s), str(t))
            done.append([str(s), str(t)])
        except OSError:
            continue
    return {"dry_run": False, "moved": len(done), "by_folder": summary, "moves": done}
