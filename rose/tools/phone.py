"""Android phone control over ADB (wireless debugging, e.g. through Tailscale).

Setup on the phone: Developer options -> Wireless debugging -> on. First time: "Pair device with pairing
code" and call phone_connect(address, pairing_code, pairing_address). Afterwards phone_connect(address)
with the IP:port shown under Wireless debugging.

Taps, swipes, typing, keys and launching apps change things on the phone, so from remote clients they are
approval-gated like the laptop's mouse and keyboard (rose/guard.py GUI_TOOLS). Coordinates default to
pixels in the latest phone_screenshot preview.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from io import BytesIO
from typing import Literal

from rose.common import ToolError, require

ADDR_RE = re.compile(r"^[\w.:\[\]-]+:\d{2,5}$")
PKG_RE = re.compile(r"^[a-zA-Z][\w]*(\.[\w]+)+$")
PREVIEW_SIDE = 1280
KNOWN_APPS = {
    "whatsapp": "com.whatsapp", "youtube": "com.google.android.youtube", "chrome": "com.android.chrome",
    "gmail": "com.google.android.gm", "maps": "com.google.android.apps.maps", "spotify": "com.spotify.music",
    "instagram": "com.instagram.android", "telegram": "org.telegram.messenger", "settings": "com.android.settings",
    "play store": "com.android.vending", "photos": "com.google.android.apps.photos",
    "calendar": "com.google.android.calendar", "drive": "com.google.android.apps.docs",
    "tailscale": "com.tailscale.ipn", "camera": "com.android.camera", "clock": "com.google.android.deskclock",
}


@dataclass
class _Map:
    scale: float
    width: int
    height: int


_last: _Map | None = None


def _adb(*args: str, timeout: float = 20, binary: bool = False) -> bytes | str:
    require("adb", "sudo apt install adb")
    try:
        p = subprocess.run(["adb", *args], capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ToolError("The phone didn't answer in time (is it awake and still connected?).") from None
    if p.returncode != 0:
        err = (p.stderr or p.stdout).decode(errors="replace").strip()
        if "no devices" in err or "device offline" in err or "not found" in err:
            raise ToolError("No phone connected. Turn on Wireless debugging and call phone_connect.") from None
        raise ToolError(f"adb {args[0]} failed: {err[-300:] or 'no output'}")
    return p.stdout if binary else p.stdout.decode(errors="replace").strip()


def _shell(*args: str, timeout: float = 20) -> str:
    return _adb("shell", " ".join(shlex.quote(a) for a in args), timeout=timeout)


def phone_devices() -> list[dict]:
    """Phones connected over ADB."""
    out = _adb("devices", "-l")
    devs = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
            devs.append({"serial": parts[0], "state": parts[1], "model": info.get("model", "")})
    return devs


def phone_connect(address: str, pairing_code: str = "", pairing_address: str = "") -> str:
    """Connect to the phone's wireless debugging (IP:port); first time also pairing_code + pairing_address."""
    if not ADDR_RE.match(address):
        raise ToolError("address must look like 100.102.41.115:37099 (IP:port from Wireless debugging).")
    if pairing_code:
        if not re.fullmatch(r"\d{6}", pairing_code) or not ADDR_RE.match(pairing_address or ""):
            raise ToolError("Pairing needs the 6-digit code and the pairing IP:port shown next to it.")
        _adb("pair", pairing_address, pairing_code, timeout=30)
    out = _adb("connect", address, timeout=20)
    if "connected" not in out:
        raise ToolError(f"Couldn't connect: {out[-200:]}")
    return f"Phone connected at {address}."


def phone_screenshot() -> list:
    """Phone screenshot (preview image + size). phone_tap/phone_swipe take coordinates in this preview."""
    global _last
    from mcp.server.mcpserver import Image
    from PIL import Image as PILImage

    png = _adb("exec-out", "screencap", "-p", binary=True, timeout=30)
    try:
        img = PILImage.open(BytesIO(png)).convert("RGB")
    except Exception:
        raise ToolError("The phone sent no image (screen locked or protected content?).") from None
    full_w = img.width
    img.thumbnail((PREVIEW_SIDE, PREVIEW_SIDE))
    _last = _Map(full_w / img.width, img.width, img.height)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=75)
    return [f"Phone screen {full_w}x{round(img.height * _last.scale)}; preview {img.width}x{img.height} "
            f"(1 preview px = {_last.scale:.3f} phone px).", Image(data=buf.getvalue(), format="jpeg")]


def _to_phone(x: float, y: float, coords: str) -> tuple[int, int]:
    if coords == "phone":
        return round(x), round(y)
    if _last is None:
        raise ToolError("Take a phone_screenshot first, or pass coords='phone'.")
    if not (0 <= x <= _last.width and 0 <= y <= _last.height):
        raise ToolError(f"({x}, {y}) is outside the last phone screenshot ({_last.width}x{_last.height}).")
    return round(x * _last.scale), round(y * _last.scale)


def phone_tap(x: float, y: float, coords: Literal["screenshot", "phone"] = "screenshot") -> str:
    """Tap the phone screen at (x, y) (latest phone_screenshot pixels by default)."""
    px, py = _to_phone(x, y, coords)
    _shell("input", "tap", str(px), str(py))
    return f"Tapped the phone at ({px}, {py})."


def phone_swipe(x1: float, y1: float, x2: float, y2: float, duration_ms: int = 300,
                coords: Literal["screenshot", "phone"] = "screenshot") -> str:
    """Swipe on the phone from (x1, y1) to (x2, y2)."""
    a, b = _to_phone(x1, y1, coords), _to_phone(x2, y2, coords)
    _shell("input", "swipe", *map(str, (*a, *b, max(50, min(int(duration_ms), 3000)))))
    return f"Swiped on the phone from {a} to {b}."


def phone_type(text: str) -> str:
    """Type text into the focused field on the phone (no Enter)."""
    if not text or len(text) > 1000:
        raise ToolError("Text must be 1-1000 characters.")
    if "\n" in text:
        raise ToolError("No newlines: use phone_key('enter') (needs confirm) to submit.")
    _shell("input", "text", text.replace(" ", "%s"))
    return f"Typed {len(text)} characters on the phone."


def phone_key(key: Literal["back", "home", "recents", "enter", "volume_up", "volume_down", "power"],
              confirm: bool = False) -> str:
    """Press a phone key. enter (may send/submit) needs confirm=true."""
    if key == "enter" and not confirm:
        raise ToolError("Enter may send a message or submit something. Ask the user, then call with confirm=true.")
    code = {"back": "KEYCODE_BACK", "home": "KEYCODE_HOME", "recents": "KEYCODE_APP_SWITCH",
            "enter": "KEYCODE_ENTER", "volume_up": "KEYCODE_VOLUME_UP", "volume_down": "KEYCODE_VOLUME_DOWN",
            "power": "KEYCODE_POWER"}[key]
    _shell("input", "keyevent", code)
    return f"Pressed {key} on the phone."


def phone_open_app(app: str) -> str:
    """Open a phone app by name (e.g. "whatsapp") or package id."""
    q = app.strip().lower()
    pkg = KNOWN_APPS.get(q) or (app.strip() if PKG_RE.match(app.strip()) else None)
    if pkg is None:
        packages = [line.removeprefix("package:") for line in _shell("pm", "list", "packages").splitlines()]
        hits = [p for p in packages if q.replace(" ", "") in p.lower()]
        if not hits:
            raise ToolError(f"No phone app matches '{app}'.")
        if len(hits) > 1:
            exact = [p for p in hits if p.lower().split(".")[-1] == q.replace(" ", "")]
            if len(exact) != 1:
                raise ToolError(f"'{app}' matches several apps: {', '.join(hits[:8])}")
            hits = exact
        pkg = hits[0]
    out = _shell("monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
    if "No activities found" in out or "Error" in out:
        raise ToolError(f"Couldn't launch {pkg}.")
    return f"Opened {pkg} on the phone."
