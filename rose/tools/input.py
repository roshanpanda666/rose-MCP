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
from rose.tools.apps import active_window as _active
from rose.tools.apps import focus_window
from rose.tools.apps import window_matches as _matches

_ENTER_KEYS = re.compile(r"(^|\+)(return|enter|kp_enter)$", re.IGNORECASE)
_KEY_RE = re.compile(r"^[A-Za-z0-9_]+(\+[A-Za-z0-9_]+)*$")

# Everyday key names -> X11 keysym names. xdotool silently IGNORES names it doesn't know (exit 0), so
# "Enter" used to be reported as pressed while nothing happened.
_KEY_ALIASES = {
    "enter": "Return", "return": "Return", "kp_enter": "KP_Enter", "esc": "Escape", "escape": "Escape",
    "backspace": "BackSpace", "del": "Delete", "delete": "Delete", "ins": "Insert", "insert": "Insert",
    "pgup": "Page_Up", "pageup": "Page_Up", "page_up": "Page_Up", "pgdn": "Page_Down", "pagedown": "Page_Down",
    "page_down": "Page_Down", "arrowup": "Up", "arrowdown": "Down", "arrowleft": "Left", "arrowright": "Right",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right", "home": "Home", "end": "End", "tab": "Tab",
    "space": "space", "spacebar": "space", "capslock": "Caps_Lock", "printscreen": "Print", "prtsc": "Print",
    "control": "ctrl", "win": "super", "windows": "super", "cmd": "super", "command": "super", "option": "alt",
}
_XDOTOOL_MODIFIERS = {"ctrl", "alt", "shift", "super", "meta"}  # xdotool's own names, not X keysyms


def _keysym_known(name: str) -> bool | None:
    """Does X11 know this keysym name? None if libX11 can't be loaded (then xdotool decides)."""
    try:
        import ctypes
        import ctypes.util

        lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("X11") or "libX11.so.6")
        lib.XStringToKeysym.restype = ctypes.c_ulong
        return lib.XStringToKeysym(name.encode()) != 0
    except Exception:
        return None


def _normalize_keys(combo: str) -> str:
    """'ctrl+Enter' -> 'ctrl+Return', 'f5' -> 'F5'; refuses names X11 doesn't know."""
    parts = []
    for part in combo.split("+"):
        low = part.lower()
        name = _KEY_ALIASES.get(low) or (low if low in _XDOTOOL_MODIFIERS else None) \
            or (part.upper() if re.fullmatch(r"[fF]\d{1,2}", part) else part)
        if name not in _XDOTOOL_MODIFIERS and _keysym_known(name) is False:
            raise ToolError(f"Unknown key '{part}' in '{combo}'. Use X11 names like Return, Escape, BackSpace, "
                            "Delete, Tab, Page_Down, Up, F5, ctrl+l. Nothing was pressed.")
        parts.append(name)
    return "+".join(parts)


def _check() -> None:
    if session_type() != "x11":
        raise ToolError("Input tools currently support X11 only.")
    if not load_config().get("input_enabled", True):
        raise ToolError("Input control is disabled (input_enabled=false in config.toml).")
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
    """Click at (x, y), in latest screenshot-preview pixels by default."""
    _check()
    sx, sy = _to_screen(x, y, coords)
    btn = {"left": "1", "middle": "2", "right": "3"}[button]
    clicks = max(1, min(clicks, 3))
    _move(sx, sy)
    run(["xdotool", "click", "--repeat", str(clicks), "--delay", "80", btn])
    return f"{button.capitalize()}-clicked x{clicks} at screen ({sx}, {sy})."


def mouse_move(x: float, y: float, coords: Literal["screenshot", "screen"] = "screenshot") -> str:
    """Move the mouse pointer to (x, y) without clicking (hover). Screenshot-preview pixels by default."""
    _check()
    sx, sy = _to_screen(x, y, coords)
    _move(sx, sy)
    return f"Moved the mouse to screen ({sx}, {sy}). Not clicked."


def scroll(
    x: float,
    y: float,
    direction: Literal["up", "down", "left", "right"] = "down",
    amount: int = 3,
    coords: Literal["screenshot", "screen"] = "screenshot",
) -> str:
    """Scroll at (x, y) by amount notches."""
    _check()
    sx, sy = _to_screen(x, y, coords)
    btn = {"up": "4", "down": "5", "left": "6", "right": "7"}[direction]
    amount = max(1, min(amount, 25))
    _move(sx, sy)
    run(["xdotool", "click", "--repeat", str(amount), "--delay", "40", btn])
    return f"Scrolled {direction} {amount} at screen ({sx}, {sy})."


def _ensure_active(window: str) -> dict:
    """Focus-verify: make `window` (id or part of its title) the active window, or refuse."""
    act = _active()
    if act and _matches(act, window):
        return act
    focus_window(window)  # raises if no/ambiguous match
    for _ in range(15):  # poll finely: most windows activate in <100 ms (same 0.75 s ceiling as before)
        time.sleep(0.05)
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
    """Type into the focused window; newlines need confirm=true. Pass window= to focus-verify first.
    Reports where text went and read-back."""
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
        # Read back early; only if the app hasn't shown the text yet, give it a little longer.
        back = ""
        for wait in (0.03, 0.15, 0.3):
            time.sleep(wait)
            back = _readback(text)
            if "READ-BACK FAILED" not in back:
                break
        msg += back
    return msg


def press_keys(keys: str, confirm: bool = False, window: str = "") -> str:
    """Press keys xdotool-style ('ctrl+l', 'alt+Tab'; space-separated for several). Enter needs confirm=true.
    window= focus-verifies first."""
    _check()
    presses = keys.split()
    if not presses or len(presses) > 20:
        raise ToolError("Give 1-20 key presses.")
    for p in presses:
        if not _KEY_RE.match(p):
            raise ToolError(f"Invalid key '{p}'. Use names like ctrl+l, Escape, Tab, Return, F5.")
    presses = [_normalize_keys(p) for p in presses]  # all validated before anything is pressed
    for p in presses:
        if _ENTER_KEYS.search(p) and not confirm:
            raise ToolError("Pressing Enter may send a message or submit something. "
                            "Ask the user, then call again with confirm=true.")
    if window:
        _ensure_active(window)
    for p in presses:
        run(["xdotool", "key", "--clearmodifiers", p])
        time.sleep(0.05)
    after = _active()
    return f"Pressed {' '.join(presses)}." + (f" Active window now: '{after['title'][:80]}'." if after else "")


def mouse_position() -> str:
    """Pointer position (screen and last-screenshot pixels)."""
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
