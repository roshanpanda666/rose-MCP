"""Accessibility-based UI control: list, read, click and fill controls by NAME (no screenshots).

Uses AT-SPI through the system Python (rose/atspi_helper.py). Works with GTK/GNOME apps out of the box;
Chromium/Electron apps (Brave, VS Code, Slack...) expose their UI only when accessibility is on
(set_quick_setting is not needed: `ui_enable_app_accessibility` turns it on for new app launches).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from rose.common import ToolError, display_env, run

HELPER = Path(__file__).resolve().parent.parent / "atspi_helper.py"
SYSTEM_PY = "/usr/bin/python3"


def _call(req: dict, timeout: float = 20) -> dict:
    try:
        p = subprocess.run([SYSTEM_PY, str(HELPER)], input=json.dumps(req), capture_output=True, text=True,
                           timeout=timeout, env=display_env())
    except subprocess.TimeoutExpired:
        raise ToolError("The app didn't answer accessibility queries in time (it may be busy or frozen).")
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise ToolError(f"Accessibility helper failed: {(p.stderr or p.stdout)[-300:]}")
    if not out.pop("ok"):
        raise ToolError(out["error"])
    return out


def ui_list(window: str, max_elements: int = 150) -> dict:
    """List a window's buttons, fields, links, menu items etc. by name with ids, WITHOUT a screenshot.
    `window` = part of its title or the app name. Then use ui_click / ui_fill with a name or id."""
    return _call({"op": "list", "window": window, "max": max(10, min(max_elements, 400))})


def ui_read(window: str, max_chars: int = 6000) -> dict:
    """Read the visible text of a window (labels, fields, messages) without a screenshot."""
    return _call({"op": "read", "window": window, "max_chars": max(500, min(max_chars, 20000))})


def ui_click(window: str, element: str) -> str:
    """Click/press/activate a control by its name (e.g. "Send", "Save") or its id from ui_list (e.g. "#0.3.1")."""
    out = _call({"op": "click", "window": window, "element": element})
    if "done" in out:
        return out["done"]
    x, y = out["click_at"]  # no accessible action: click its centre
    run(["xdotool", "mousemove", str(x), str(y)])
    run(["xdotool", "click", "1"])
    return f"Clicked {out['element']} at ({x}, {y})."


def ui_fill(window: str, element: str, text: str) -> str:
    """Put text into a field by name or id (replaces its contents). Doesn't press Enter or submit."""
    if len(text) > 5000:
        raise ToolError("Text too long (max 5000).")
    out = _call({"op": "set_text", "window": window, "element": element, "text": text})
    if "done" in out:
        return out["done"]
    run(["xdotool", "key", "--clearmodifiers", "ctrl+a"])  # field focused: replace by typing
    run(["xdotool", "type", "--delay", "8", "--", text], timeout=max(15, len(text) * 0.05))
    return f"Typed into {out['focused_for_typing']}."


def ui_enable_app_accessibility(enabled: bool = True) -> str:
    """Make Chromium/Electron apps (Brave, VS Code, Slack, ...) expose their UI to ui_list/ui_click.
    Takes effect for apps started after this (restart the app)."""
    run(["gsettings", "set", "org.gnome.desktop.interface", "toolkit-accessibility", "true" if enabled else "false"])
    return f"App accessibility {'on' if enabled else 'off'}; restart the app for it to apply."
