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
