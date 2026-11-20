"""Mouse and keyboard control via xdotool (X11).

Coordinates default to the most recent screenshot preview (what Claude sees); ROSE converts them
to real screen pixels. Anything that would press Enter (which sends messages / submits forms in most
apps) needs confirm=true.
"""

from __future__ import annotations

import re
import time
from typing import Literal

from rose.common import ToolError, require, run, session_type
from rose.config import load_config
from rose.tools import screenshot

_ENTER_KEYS = re.compile(r"(^|\+)(return|enter|kp_enter)$", re.IGNORECASE)
_KEY_RE = re.compile(r"^[A-Za-z0-9_]+(\+[A-Za-z0-9_]+)*$")


def _check() -> None:
    if session_type() != "x11":
        raise ToolError("Input tools currently support X11 only.")
    if not load_config().get("input_enabled", True):
        raise ToolError("Input control is disabled (input_enabled=false in ~/rose/config.json).")
    require("xdotool", "sudo apt install xdotool")


def _to_screen(x: float, y: float, coords: str) -> tuple[int, int]:
    if coords == "screen":
        return round(x), round(y)
    m = screenshot.last_mapping
    if m is None:
        raise ToolError("No screenshot yet. Take one first, or pass coords='screen'.")
    if not (0 <= x <= m.width and 0 <= y <= m.height):
        raise ToolError(f"({x}, {y}) is outside the last screenshot ({m.width}x{m.height}).")
    return round(m.offset_x + x * m.scale), round(m.offset_y + y * m.scale)


def _move(sx: int, sy: int) -> None:
    # No `--sync`: it waits for a pointer-motion event, which never comes if the pointer is already
    # at (sx, sy), so it would hang until the timeout. Move, then give the server a moment.
    run(["xdotool", "mousemove", str(sx), str(sy)])
    time.sleep(0.05)


def mouse_click(
    x: float,
    y: float,
    button: Literal["left", "right", "middle"] = "left",
    clicks: int = 1,
    coords: Literal["screenshot", "screen"] = "screenshot",
) -> str:
    """Click at (x, y). By default x/y are pixels in the latest screenshot preview."""
    _check()
    sx, sy = _to_screen(x, y, coords)
    btn = {"left": "1", "middle": "2", "right": "3"}[button]
    clicks = max(1, min(clicks, 3))
    _move(sx, sy)
    run(["xdotool", "click", "--repeat", str(clicks), "--delay", "80", btn])
    return f"{button.capitalize()}-clicked x{clicks} at screen ({sx}, {sy})."


def scroll(
    x: float,
    y: float,
    direction: Literal["up", "down", "left", "right"] = "down",
    amount: int = 3,
    coords: Literal["screenshot", "screen"] = "screenshot",
) -> str:
    """Scroll at (x, y) by `amount` notches."""
    _check()
    sx, sy = _to_screen(x, y, coords)
    btn = {"up": "4", "down": "5", "left": "6", "right": "7"}[direction]
    amount = max(1, min(amount, 25))
    _move(sx, sy)
    run(["xdotool", "click", "--repeat", str(amount), "--delay", "40", btn])
    return f"Scrolled {direction} {amount} at screen ({sx}, {sy})."


def type_text(text: str, confirm: bool = False) -> str:
    """Type text into the focused window. Text with newlines (which press Enter) needs confirm=true."""
    _check()
    if ("\n" in text or "\r" in text) and not confirm:
        raise ToolError("This text contains newlines, which press Enter and may send or submit. "
                        "Ask the user, then call again with confirm=true (or paste via clipboard).")
    if len(text) > 5000:
        raise ToolError("Text too long to type (max 5000 chars); use clipboard_write and press ctrl+v.")
    run(["xdotool", "type", "--delay", "8", "--", text], timeout=max(15, len(text) * 0.05))
    return f"Typed {len(text)} characters."


def press_keys(keys: str, confirm: bool = False) -> str:
    """Press a key or chord, xdotool style: 'ctrl+l', 'alt+Tab', 'Escape', 'ctrl+shift+t'.
    Space-separate several presses: 'ctrl+a ctrl+c'. Any Enter/Return press needs confirm=true."""
    _check()
    presses = keys.split()
    if not presses or len(presses) > 20:
        raise ToolError("Give 1-20 key presses.")
    for p in presses:
        if not _KEY_RE.match(p):
            raise ToolError(f"Invalid key '{p}'. Use names like ctrl+l, Escape, Tab, Return, F5.")
        if _ENTER_KEYS.search(p) and not confirm:
            raise ToolError("Pressing Enter may send a message or submit something. "
                            "Ask the user, then call again with confirm=true.")
    for p in presses:
        run(["xdotool", "key", "--clearmodifiers", p])
        time.sleep(0.05)
    return f"Pressed {keys}."


def mouse_position() -> str:
    """Current pointer position in screen pixels (and in the last screenshot, if it's on it)."""
    _check()
    out = run(["xdotool", "getmouselocation", "--shell"]).decode()
    v = dict(line.split("=", 1) for line in out.split() if "=" in line)
    sx, sy = int(v["X"]), int(v["Y"])
    m = screenshot.last_mapping
    if m:
        px, py = (sx - m.offset_x) / m.scale, (sy - m.offset_y) / m.scale
        if 0 <= px <= m.width and 0 <= py <= m.height:
            return f"Screen ({sx}, {sy}); last screenshot ({px:.0f}, {py:.0f})."
    return f"Screen ({sx}, {sy})."
