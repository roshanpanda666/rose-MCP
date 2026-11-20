"""Housekeeping: delete old screenshots, webcam photos, logs, approvals and Forge jobs; report disk use.

Retention (days) comes from config: keep_screenshots_days, keep_logs_days. Runs daily from `rose watch`,
on demand with `rose cleanup`, and its numbers show on the dashboard.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from rose.common import _log
from rose.config import LOG_DIR, ROSE_HOME, SCREENSHOT_DIR, load_config

MAX_TEXT_LOG = 5 * 1024 * 1024  # http.log / tunnel.log are trimmed to their last 5 MB


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


def disk_usage() -> dict:
    """Bytes used by each part of ~/rose."""
    parts = {"screenshots": SCREENSHOT_DIR, "webcam": ROSE_HOME / "webcam", "logs": LOG_DIR,
             "forge_jobs": ROSE_HOME / "forge" / "jobs", "approvals": ROSE_HOME / "approvals",
             "bridge": ROSE_HOME / "bridge"}
    out = {k: _size(v) for k, v in parts.items()}
    out["total"] = _size(ROSE_HOME)
    return out


def _older_than(paths, days: float) -> list[Path]:
    cutoff = time.time() - days * 86400
    return [p for p in paths if p.exists() and p.stat().st_mtime < cutoff]


def cleanup(dry_run: bool = False) -> dict:
    cfg = load_config()
    shots = float(cfg.get("keep_screenshots_days", 7))
    logs = float(cfg.get("keep_logs_days", 30))
    doomed: dict[str, list[Path]] = {
        "screenshots": _older_than(SCREENSHOT_DIR.glob("*.png"), shots),
        "webcam": _older_than((ROSE_HOME / "webcam").glob("*.jpg"), shots),
        "logs": _older_than(LOG_DIR.glob("*.jsonl"), logs),
        "approvals": _older_than((ROSE_HOME / "approvals").glob("*.json"), 2),
        "forge_jobs": _older_than((ROSE_HOME / "forge" / "jobs").glob("*/"), 14),
    }
    freed = 0
    counts = {}
    for kind, paths in doomed.items():
        counts[kind] = len(paths)
        for p in paths:
            freed += _size(p)
            if not dry_run:
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    # trim the plain-text logs that only ever grow
    for name in ("http.log", "tunnel.log", "forge.jsonl"):
        p = LOG_DIR / name
        if p.exists() and p.stat().st_size > MAX_TEXT_LOG:
            freed += p.stat().st_size - MAX_TEXT_LOG
            if not dry_run:
                data = p.read_bytes()[-MAX_TEXT_LOG:]
                p.write_bytes(data[data.find(b"\n") + 1:])
            counts[name] = "trimmed"
    result = {"deleted": counts, "freed_mb": round(freed / 1024**2, 1), "dry_run": dry_run}
    if not dry_run and freed:
        _log({"event": "cleanup", **result})
    return result
