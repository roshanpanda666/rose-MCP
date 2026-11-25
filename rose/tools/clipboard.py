"""Clipboard tools: wl-clipboard on Wayland, xclip on X11."""

from __future__ import annotations

import subprocess

from rose.common import ToolError, display_env, require, run, session_type


def clipboard_read() -> str:
    """Clipboard text."""
    if session_type() == "wayland":
        require("wl-paste", "sudo apt install wl-clipboard")
        cmd = ["wl-paste", "--no-newline"]
    else:
        require("xclip", "sudo apt install xclip")
        cmd = ["xclip", "-selection", "clipboard", "-o"]
    try:
        return run(cmd).decode(errors="replace")
    except ToolError as e:
        # Both tools exit non-zero when the clipboard is empty or holds non-text data.
        if "Nothing is copied" in str(e) or "target STRING not available" in str(e) or "No selection" in str(e):
            return ""
        raise


def clipboard_write(text: str) -> str:
    """Set clipboard text."""
    if session_type() == "wayland":
        require("wl-copy", "sudo apt install wl-clipboard")
        cmd = ["wl-copy"]
    else:
        require("xclip", "sudo apt install xclip")
        cmd = ["xclip", "-selection", "clipboard", "-i"]
    # xclip and wl-copy fork a background process that keeps owning the clipboard.
    # That child inherits any pipes, so stdout/stderr must not be captured or we'd wait forever.
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=display_env(),
    )
    proc.communicate(text.encode(), timeout=10)
    if proc.returncode != 0:
        raise ToolError(f"{cmd[0]} failed with exit code {proc.returncode}")
    return f"Copied {len(text)} characters to the clipboard."


# ---------------- history ----------------
# rose-watch polls the clipboard (config `clipboard_history`, default on) and keeps the last KEEP text clips in
# ~/rose/clipboard_history.json (mode 600). Clips that look like passwords, keys or tokens are never stored, nor
# anything a password manager marks as secret.

import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402
from typing import Literal  # noqa: E402

from rose.config import ROSE_HOME, load_config  # noqa: E402

HISTORY = ROSE_HOME / "clipboard_history.json"
KEEP = 50
MAX_CLIP = 20_000
SECRET_PREFIX = re.compile(r"^(sk-|sk_|pk_|rk_|ghp_|gho_|ghs_|github_pat_|glpat-|xox[abpr]-|AKIA|AIza|eyJ|"
                           r"-----BEGIN|ya29\.|npm_|hf_|gsk_)")


def looks_secret(text: str) -> bool:
    t = text.strip()
    if SECRET_PREFIX.match(t):
        return True
    if re.search(r"(?i)\b(password|passwd|secret|api[_-]?key|token)\s*[:=]", t):
        return True
    # One "word" mixing letters with digits/symbols, no spaces: a password or key.
    if 8 <= len(t) <= 200 and not re.search(r"\s", t) and re.search(r"[A-Za-z]", t) \
            and re.search(r"[0-9]", t) and not re.match(r"^(https?://|/|~/|\./)", t) \
            and not re.match(r"^[\w.+-]+@[\w-]+\.[\w.]+$", t):
        return True
    return False


def _marked_secret() -> bool:
    """Password managers (KeePassXC, Bitwarden...) flag secret clips with this target."""
    if session_type() == "wayland":
        cmd = ["wl-paste", "--list-types"]
    else:
        cmd = ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=3, env=display_env()).stdout
    except Exception:
        return False
    return "x-kde-passwordManagerHint" in out


def _load_history() -> list[dict]:
    try:
        return json.loads(HISTORY.read_text())
    except (OSError, ValueError):
        return []


def _save_history(items: list[dict]) -> None:
    ROSE_HOME.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(items[-KEEP:], f)
    tmp.replace(HISTORY)


def record_clip(text: str) -> bool:
    """Add a clip to the history unless empty, huge, secret-looking or the same as the newest."""
    if not text.strip() or len(text) > MAX_CLIP or looks_secret(text):
        return False
    items = _load_history()
    h = hashlib.sha1(text.encode()).hexdigest()
    if items and items[-1].get("hash") == h:
        return False
    items = [i for i in items if i.get("hash") != h]
    items.append({"hash": h, "time": time.strftime("%Y-%m-%d %H:%M"), "text": text})
    _save_history(items)
    return True


def history_loop(interval: float = 2.0) -> None:
    """rose-watch thread: poll the clipboard and record new text clips."""
    last = None
    while True:
        try:
            if load_config().get("clipboard_history", True):
                text = clipboard_read()
                h = hashlib.sha1(text.encode()).hexdigest()
                if h != last:
                    last = h
                    if text and not _marked_secret():
                        record_clip(text)
        except Exception:
            pass
        time.sleep(interval)


def clipboard_history(action: Literal["list", "copy", "clear"] = "list", n: int = 1, query: str = "",
                      limit: int = 10) -> list[dict] | str:
    """Recent clipboard text (newest = 1; passwords/keys are never kept). list: newest `limit` (query filters).
    copy: put clip number n back on the clipboard ('what I copied before that' = n=2). clear: wipe the history."""
    if action == "clear":
        _save_history([])
        return "Clipboard history cleared."
    items = list(reversed(_load_history()))
    if query:
        q = query.lower()
        items = [i for i in items if q in i["text"].lower()]
    if action == "copy":
        if not 1 <= n <= len(items):
            raise ToolError(f"No clip {n}; there are {len(items)}.")
        clipboard_write(items[n - 1]["text"])
        return f"Clip {n} (from {items[n - 1]['time']}) is on the clipboard again."
    if not items:
        return [{"none": "No clipboard history yet." if not query else f"No clip contains '{query}'."}]
    out = []
    for k, i in enumerate(items[: max(1, min(limit, KEEP))], 1):
        t = i["text"]
        out.append({"n": k, "time": i["time"], "chars": len(t),
                    "text": t if len(t) <= 300 else t[:300] + "…"})
    return out
