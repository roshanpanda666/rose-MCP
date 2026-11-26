"""Shared helpers: tool-call logging, display detection, running commands, small HTTP/systemd utilities."""

from __future__ import annotations

import contextlib
import functools
import inspect
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any


def _sdk_exceptions():
    """The MCP SDK's exceptions module WITHOUT importing the SDK package (its __init__ costs ~0.55 s, paid by every
    `rose` CLI call, e.g. the Super+R talk hotkey). The module is registered under its real name, so when the server
    later imports the SDK it reuses this very module: ToolError stays the SDK's class (its message reaches the
    client). Falls back to a normal import if the layout ever changes."""
    import importlib
    import importlib.util
    import sys

    name = "mcp.server.mcpserver.exceptions"
    if name in sys.modules or "mcp.server.mcpserver" in sys.modules:
        return importlib.import_module(name)
    try:
        root = importlib.util.find_spec("mcp")
        path = os.path.join(root.submodule_search_locations[0], "server", "mcpserver", "exceptions.py")
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        mod.ToolError  # noqa: B018  (layout check)
        return mod
    except Exception:  # noqa: BLE001
        sys.modules.pop(name, None)
        return importlib.import_module(name)


_SDKToolError = _sdk_exceptions().ToolError

from rose.config import LOG_DIR


class ToolError(_SDKToolError):
    """Raised for expected failures. The SDK passes the message through to Claude;
    any other exception is reported only as 'Error executing tool <name>'."""


def _log(entry: dict) -> None:
    """Append one JSON line to today's log (~/rose/logs/YYYY-MM-DD.jsonl)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{datetime.now():%Y-%m-%d}.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _short(value: Any, limit: int = 300) -> str:
    s = value if isinstance(value, str) else repr(value)
    return s if len(s) <= limit else s[:limit] + f"... ({len(s)} chars)"


# Observation kinds, so the logs show how often ROSE falls back to screenshots (see rose/metrics.py).
KIND = {
    **dict.fromkeys(("ui_list", "ui_read", "ui_click", "ui_fill", "browser_read", "browser_tabs", "browser_status",
                     "browser_click", "browser_fill", "browser_press", "list_windows"), "tree"),
    "take_screenshot": "screenshot",
    **dict.fromkeys(("mouse_click", "mouse_move", "scroll"), "coords"),
    "click_on_screen": "tree",  # located by tree/OCR, not guessed pixels
    **dict.fromkeys(("type_text", "press_keys"), "keys"),
}


def logged(fn: Callable) -> Callable:
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
            **({"kind": KIND[fn.__name__]} if fn.__name__ in KIND else {}),
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


def display_env() -> dict[str, str]:
    """Environment for GUI commands. Claude may launch ROSE without DISPLAY set, so fall back to :0."""
    env = dict(os.environ)
    if session_type() == "x11" and not env.get("DISPLAY"):
        env["DISPLAY"] = ":0"
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{os.getuid()}/bus"
    return env


def launch_detached(cmd: list[str], cwd: str | None = None, name: str = "") -> subprocess.Popen:
    """Start a desktop app in its own systemd --user scope, so it never lives in rose.service's cgroup
    (a ROSE restart would otherwise kill it: KillMode=control-group). Falls back to a plain detached process."""
    env = display_env()
    full = list(cmd)
    if shutil.which("systemd-run") and os.environ.get("ROSE_NO_SCOPE") != "1":
        tag = "".join(c if c.isalnum() else "-" for c in (name or os.path.basename(cmd[0])))[:40].strip("-") or "app"
        full = ["systemd-run", "--user", "--scope", "--collect", "--quiet",
                f"--unit=app-rose-{tag}-{os.getpid()}-{time.monotonic_ns()}", "--", *cmd]
    return subprocess.Popen(full, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def require(binary: str, hint: str) -> str:
    """Path of `binary`, or a ToolError telling the user how to install it."""
    path = shutil.which(binary)
    if not path:
        raise ToolError(f"'{binary}' is not installed. Install it with: {hint}")
    return path


def run(cmd: list[str], *, input: bytes | None = None, timeout: float = 15) -> bytes:
    """Run a command (no shell) in the desktop environment; return stdout or raise ToolError."""
    try:
        proc = subprocess.run(cmd, input=input, capture_output=True, timeout=timeout, env=display_env())
    except subprocess.TimeoutExpired:
        raise ToolError(f"{cmd[0]} timed out after {timeout}s") from None
    except FileNotFoundError:
        raise ToolError(f"'{cmd[0]}' is not installed.") from None
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()
        raise ToolError(f"{cmd[0]} failed (exit {proc.returncode}): {err or 'no error output'}")
    return proc.stdout


def run_text(cmd: list[str], timeout: float = 10) -> str:
    """`run` decoded as text and stripped."""
    return run(cmd, timeout=timeout).decode(errors="replace").strip()


def notify_desktop(title: str, body: str = "", urgency: str = "normal") -> None:
    """Best-effort desktop notification (never raises)."""
    if not shutil.which("notify-send"):
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(["notify-send", "--app-name=ROSE", f"--urgency={urgency}", title, body[:200]],
                       env=display_env(), capture_output=True, timeout=5)


def http_status(url: str, method: str = "GET", data: bytes | None = None, headers: dict | None = None,
                timeout: float = 5) -> int | None:
    """HTTP status code of a request (errors included), or None if nothing answered."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def probe_unauth(base: str, timeout: float = 10) -> int | None:
    """Status of an MCP request without credentials. 401 = auth enforced (anything 2xx/3xx is unsafe)."""
    return http_status(f"{base}/mcp", "POST", b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
                       {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                        "User-Agent": "rose-safety-check"}, timeout=timeout)


def systemctl_user(*args: str) -> str:
    """Output of `systemctl --user ...` (stdout, else stderr); 'n/a' without systemd."""
    if not shutil.which("systemctl"):
        return "n/a"
    r = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    return r.stdout.strip() or r.stderr.strip()
