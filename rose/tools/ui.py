"""Accessibility-based UI control: list, read, click and fill controls by NAME (no screenshots).

This is ROSE's PREFERRED way to see and act on desktop apps (tree-first): observe with ui_list /
ui_read, act with ui_click / ui_fill by name or id, then read back to confirm. Screenshots are the
fallback for apps whose tree is empty (canvas apps, games, remote desktops, video).

Uses AT-SPI through the system Python (rose/atspi_helper.py). Works with GTK/GNOME apps out of the box;
Chromium/Electron apps (Brave, VS Code, Slack...) expose their UI only when accessibility is on
(`ui_enable_app_accessibility` turns it on for new app launches).
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import time
from pathlib import Path

from rose.common import ToolError, display_env, run
from rose.tools.apps import active_window as _x_active
from rose.tools.apps import focus_window

HELPER = Path(__file__).resolve().parent.parent / "atspi_helper.py"
SYSTEM_PY = "/usr/bin/python3"

# Apps that only expose a tree when toolkit accessibility is on (and after a restart).
CHROMIUM_LIKE = ("brave", "chrome", "chromium", "code", "visual studio code", "vscodium", "slack", "discord",
                 "electron", "teams", "signal", "obsidian", "spotify", "element", "whatsapp", "notion", "cursor")


def _call(req: dict, timeout: float = 20) -> dict:
    """Run one request through the AT-SPI helper (system Python) and return its reply."""
    try:
        p = subprocess.run([SYSTEM_PY, str(HELPER)], input=json.dumps(req), capture_output=True, text=True,
                           timeout=timeout, env=display_env())
    except subprocess.TimeoutExpired:
        raise ToolError("The app didn't answer accessibility queries in time (it may be busy or frozen).") from None
    except FileNotFoundError:
        raise ToolError(f"{SYSTEM_PY} not found: ui_* tools need the system Python with python3-gi.") from None
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise ToolError(f"Accessibility helper failed: {(p.stderr or p.stdout)[-300:]}") from None
    if not out.pop("ok"):
        raise ToolError(out["error"])
    return out


def _a11y_enabled() -> bool | None:
    try:
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "toolkit-accessibility"],
                             capture_output=True, text=True, timeout=5, env=display_env()).stdout.strip()
        return out == "true"
    except Exception:
        return None


def _empty_tree_hint(window: str, app: str = "") -> str:
    """What to do when a window shows no usable tree: enable accessibility for Chromium/Electron apps,
    otherwise the screenshot fallback is legitimate."""
    who = f"{window} {app}".lower()
    if any(k in who for k in CHROMIUM_LIKE):
        if _a11y_enabled() is False:
            return ("This looks like a Chromium/Electron app and app accessibility is OFF. Call "
                    "ui_enable_app_accessibility, ask the user before restarting the app (it may have unsaved "
                    "work), then ui_list again. Only fall back to take_screenshot if it's still empty.")
        return ("This is a Chromium/Electron app: if it was started before accessibility was turned on, it "
                "must be restarted (ask the user first) before its tree appears. For websites, prefer ROSE's "
                "browser (browser_read / browser_click / browser_fill). Fall back to take_screenshot only if "
                "that's not possible.")
    return ("This app exposes no usable accessibility tree (canvas app, game, remote desktop, video or no "
            "a11y support). Fall back to take_screenshot (area='active_window', include_image=false first if "
            "OCR text is enough).")


def _titles_match(a: str, b: str) -> bool:
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    return bool(a and b) and (a == b or a in b or b in a)


def ui_list(window: str, max_elements: int = 150) -> dict:
    """PREFERRED way to see an app: its controls by name/id, field values, focus. window = title part or app."""
    try:
        out = _call({"op": "list", "window": window, "max": max(10, min(max_elements, 400))})
    except ToolError as e:
        if "No accessible window" in str(e):
            raise ToolError(f"{e}\n{_empty_tree_hint(window)}") from None
        raise
    if len(out.get("elements", [])) < 3:
        out["hint"] = _empty_tree_hint(out.get("window", window), out.get("app", ""))
    return out


def ui_read(window: str, max_chars: int = 6000, element: str = "") -> dict:
    """PREFERRED way to read an app's text. element=name/id reads one control (use to read back)."""
    req = {"op": "read", "window": window, "max_chars": max(500, min(max_chars, 20000))}
    if element:
        req["element"] = element
    try:
        out = _call(req)
    except ToolError as e:
        if "No accessible window" in str(e):
            raise ToolError(f"{e}\n{_empty_tree_hint(window)}") from None
        raise
    if not element and not out.get("text", "").strip():
        out["hint"] = _empty_tree_hint(out.get("window", window), out.get("app", ""))
    return out


def ui_click(window: str, element: str) -> str:
    """PREFERRED click: press a control by name ("Save") or id ("#0.3.1"). Reports the active window after."""
    out = _call({"op": "click", "window": window, "element": element})
    if "done" in out:
        msg = out["done"]
    else:
        x, y = out["click_at"]  # no accessible action: click its centre
        run(["xdotool", "mousemove", str(x), str(y)])
        run(["xdotool", "click", "1"])
        msg = f"Clicked {out['element']} at ({x}, {y})."
    time.sleep(0.3)
    act = _x_active()
    if act:
        msg += f" Active window now: '{act['title'][:80]}'."
    return msg


def _verify_value(readback: dict | None, text: str) -> str:
    if not readback:
        return " Read-back unavailable; check with ui_read(window, element=...)."
    v = readback.get("value")
    if v == "(hidden: password field)":
        return " (password field: value not read back)."
    if v is not None and " ".join(text.split()) in " ".join(str(v).split()):
        return " Read-back OK: the field contains the text."
    return (" READ-BACK FAILED: the field does not contain the expected text. Check with ui_read(window, "
            "element=...); if text went elsewhere, undo it (ctrl+z in that field, never in a terminal) and "
            "retry; use take_screenshot only if the tree doesn't explain it.")


def ui_fill(window: str, element: str, text: str) -> str:
    """PREFERRED typing: replace a field's text by name/id (no Enter). Reads it back."""
    if len(text) > 5000:
        raise ToolError("Text too long (max 5000).")
    out = _call({"op": "set_text", "window": window, "element": element, "text": text})
    if "done" in out:
        return out["done"] + "." + _verify_value(out.get("readback"), text)

    # No editable-text interface: we must type. Verify focus first so keystrokes can't land elsewhere.
    title = out.get("window", "")
    act = _x_active()
    if act and title and not _titles_match(act["title"], title):
        with contextlib.suppress(ToolError):
            focus_window(title)
        time.sleep(0.2)
        out = {**out, **_call({"op": "focus", "window": window, "element": out["id"]})}
        act = _x_active()
    if act and title and not _titles_match(act["title"], title):
        raise ToolError(f"Not typing: the active window is '{act['title'][:80]}', not '{title[:80]}', and it "
                        f"couldn't be focused. Call focus_window, then retry.")
    if not out.get("focused"):
        raise ToolError(f"Not typing: {out['focused_for_typing']} didn't take keyboard focus. Try ui_click on "
                        f"it first, then ui_fill again.")
    run(["xdotool", "key", "--clearmodifiers", "ctrl+a"])  # field focused: replace by typing
    run(["xdotool", "type", "--delay", "8", "--", text], timeout=max(15, len(text) * 0.05))
    time.sleep(0.2)
    try:
        back = _call({"op": "read", "window": window, "element": out["id"],
                      "max_chars": max(500, min(len(text) + 200, 20000))}).get("element")
    except ToolError:
        back = None
    return f"Typed into {out['focused_for_typing']}." + _verify_value(back, text)


def ui_enable_app_accessibility(enabled: bool = True) -> str:
    """Expose Chromium/Electron app UIs (VS Code, Brave, Slack) to ui_*; app restart needed."""
    run(["gsettings", "set", "org.gnome.desktop.interface", "toolkit-accessibility", "true" if enabled else "false"])
    return f"App accessibility {'on' if enabled else 'off'}; restart the app for it to apply."
