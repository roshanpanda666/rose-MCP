"""Shared helpers: tool-call logging, display detection, running commands."""

from __future__ import annotations

import functools
import inspect
import json
import os
import shutil
import subprocess
import time
from datetime import datetime

from mcp.server.mcpserver.exceptions import ToolError as _SDKToolError

from rose.config import LOG_DIR


class ToolError(_SDKToolError):
    """Raised for expected failures. The SDK passes the message through to Claude;
    any other exception is reported only as 'Error executing tool <name>'."""


def _log(entry: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{datetime.now():%Y-%m-%d}.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _short(value, limit: int = 300):
    s = value if isinstance(value, str) else repr(value)
    return s if len(s) <= limit else s[:limit] + f"... ({len(s)} chars)"


def logged(fn):
    """Log every call (args, outcome, duration) to ~/rose/logs/YYYY-MM-DD.jsonl."""

    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        bound = sig.bind_partial(*args, **kwargs)
        start = time.monotonic()
        try:
            from rose.tools.bridge import caller  # who: Forge (local) or the remote client's label

            who = caller()
        except Exception:
            who = "?"
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "caller": who,
            "tool": fn.__name__,
            "args": {k: _short(v) for k, v in bound.arguments.items()},
        }
        try:
            result = fn(*args, **kwargs)
            entry.update(ok=True, result=_short(result))
            return result
        except Exception as e:
            entry.update(ok=False, error=f"{type(e).__name__}: {e}")
            raise
        finally:
            entry["ms"] = round((time.monotonic() - start) * 1000)
            _log(entry)

    return wrapper


def session_type() -> str:
    """'wayland' or 'x11' for the current graphical session."""
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
        return "wayland"
    return "x11"


def display_env() -> dict:
    """Environment for GUI commands. Claude may launch ROSE without DISPLAY set, so fall back to :0."""
    env = dict(os.environ)
    if session_type() == "x11" and not env.get("DISPLAY"):
        env["DISPLAY"] = ":0"
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{os.getuid()}/bus"
    return env


def require(binary: str, hint: str) -> str:
    path = shutil.which(binary)
    if not path:
        raise ToolError(f"'{binary}' is not installed. Install it with: {hint}")
    return path


def run(cmd: list[str], *, input: bytes | None = None, timeout: float = 15) -> bytes:
    try:
        proc = subprocess.run(
            cmd, input=input, capture_output=True, timeout=timeout, env=display_env()
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"{cmd[0]} timed out after {timeout}s")
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()
        raise ToolError(f"{cmd[0]} failed (exit {proc.returncode}): {err or 'no error output'}")
    return proc.stdout
