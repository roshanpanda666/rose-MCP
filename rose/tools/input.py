"""Mouse and keyboard control via xdotool (X11).

Coordinates default to the most recent screenshot preview (what Claude sees); ROSE converts them
to real screen pixels. Anything that would press Enter (which sends messages / submits forms in most
apps) needs confirm=true.

Typing is focus-verified and read back: with `window`, type_text / press_keys first make sure that window
is the active one (focusing it if needed, refusing otherwise); afterwards they report the active window
and (type_text) check through the accessibility tree that the text landed in the focused element.
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


def _active() -> dict | None:
    try:
        from rose.tools.apps import list_windows
        return next((w for w in list_windows() if w.get("active")), None)
    except Exception:
        return None


def _matches(win: dict, window: str) -> bool:
    w = window.strip().lower()
    if w.startswith("0x") or w.isdigit():
        try:
            return int(win["id"], 16) == (int(w, 16) if w.startswith("0x") else int(w))
        except ValueError:
            return False
    return w in (win.get("title") or "").lower() or w == (win.get("app") or "").lower()


def _ensure_active(window: str) -> dict:
    """Focus-verify: make `window` (id or part of its title) the active window, or refuse."""
    act = _active()
    if act and _matches(act, window):
        return act
    from rose.tools.apps import focus_window
    focus_window(window)  # raises if no/ambiguous match
    for _ in range(5):
        time.sleep(0.15)
        act = _active()
        if act and _matches(act, window):
            return act
    raise ToolError(f"Not typing: couldn't make '{window}' the active window (active is "
                    f"'{(act or {}).get('title', '?')[:80]}'). Nothing was typed.")


def _readback(text: str) -> str:
    """Check via AT-SPI that the focused element of the active window now contains `text`."""
    probe = max(text.replace("\r", "\n").split("\n"), key=len).strip()[-200:]
    if not probe:
        return ""
    try:
        from rose.tools.ui import _call
        r = _call({"op": "focused", "expect": probe}, timeout=10)
    except Exception:
        return " Read-back unavailable (accessibility tree not reachable)."
    el = r.get("element")
    if not el:
        return (" Read-back unavailable: no focused element in the accessibility tree (app without a11y?). "
                "Verify with ui_read, or a screenshot if the tree is empty.")
    where = f"{el.get('role')} '{(el.get('name') or '')[:50]}'"
    if r.get("contains") is True:
        return f" Read-back OK: text found in the focused {where}."
    if r.get("contains") is None:
        return f" Focused element is a {where} (password field: not read back)."
    if not r.get("has_text"):
        return f" Read-back unavailable: the focused {where} doesn't expose its text; verify with ui_read."
    return (f" READ-BACK FAILED: the focused {where} doesn't contain the typed text. It may have landed "
            "elsewhere: check with ui_read; if it's in the wrong field, undo (ctrl+z, never in a terminal), "
            "focus the right place (prefer ui_fill) and retry. Use take_screenshot only if the tree can't explain it.")


def type_text(text: str, confirm: bool = False, window: str = "", verify: bool = True) -> str:
    """Type text into the focused window. Text with newlines (which press Enter) needs confirm=true.
    Verify the target window is active first: pass `window` (id or part of its title) and ROSE focuses it
    and refuses to type if it can't. Prefer ui_fill when the field is in the tree (ui_list). Afterwards
    the result says which window got the text and whether read-back found it in the focused element."""
    _check()
    if ("\n" in text or "\r" in text) and not confirm:
        raise ToolError("This text contains newlines, which press Enter and may send or submit. "
                        "Ask the user, then call again with confirm=true (or paste via clipboard).")
    if len(text) > 5000:
        raise ToolError("Text too long to type (max 5000 chars); use clipboard_write and press ctrl+v.")
    before = _ensure_active(window) if window else _active()
    run(["xdotool", "type", "--delay", "8", "--", text], timeout=max(15, len(text) * 0.05))
    msg = f"Typed {len(text)} characters"
    if before:
        msg += f" into '{before['title'][:80]}'"
    msg += "."
    after = _active()
    if before and after and after["id"] != before["id"]:
        msg += (f" WARNING: the active window changed to '{after['title'][:80]}' while typing; some text may "
                "have landed there. Check with ui_read.")
    if verify:
        time.sleep(0.15)
        msg += _readback(text)
    return msg


def press_keys(keys: str, confirm: bool = False, window: str = "") -> str:
    """Press a key or chord, xdotool style: 'ctrl+l', 'alt+Tab', 'Escape', 'ctrl+shift+t'.
    Space-separate several presses: 'ctrl+a ctrl+c'. Any Enter/Return press needs confirm=true.
    Pass `window` (id or part of its title) to make sure keys go to that window: ROSE focuses it first
    and refuses if it can't. The result names the active window afterwards."""
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
    if window:
        _ensure_active(window)
    for p in presses:
        run(["xdotool", "key", "--clearmodifiers", p])
        time.sleep(0.05)
    after = _active()
    return f"Pressed {keys}." + (f" Active window now: '{after['title'][:80]}'." if after else "")


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
