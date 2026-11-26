"""Android phone control over ADB (wireless debugging, e.g. through Tailscale).

Setup on the phone: Developer options -> Wireless debugging -> on. First time: "Pair device with pairing
code" and call phone_connect(address, pairing_code, pairing_address). Afterwards phone_connect(address)
with the IP:port shown under Wireless debugging.

Taps, swipes, typing, keys and launching apps change things on the phone, so from remote clients they are
approval-gated like the laptop's mouse and keyboard (rose/guard.py GUI_TOOLS). Coordinates default to
pixels in the latest phone_screenshot preview.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
import socket
import subprocess
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Literal

from rose.common import ToolError, launch_detached, require
from rose.config import ROSE_HOME, load_config

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
_server_started = False


def _ensure_server() -> None:
    """Start the adb server daemon in its own scope once, so a ROSE restart doesn't kill it (and the phone link)."""
    global _server_started
    if _server_started:
        return
    try:
        launch_detached(["adb", "start-server"], name="adb").wait(timeout=15)  # no-op if already running
    except Exception:
        pass  # adb will start its own server on the next call
    _server_started = True


NO_DEVICE_CMDS = {"devices", "connect", "disconnect", "pair", "start-server", "kill-server", "version"}


SERIAL_TTL = 10.0  # seconds a device pick is reused (saves an `adb devices` per action)
RECONNECT_GAP = 15.0  # min seconds between automatic reconnect attempts
LAST_FILE = ROSE_HOME / "phone_last.json"
REMOTE_PORT = 5555  # fixed `adb tcpip` port: reachable over Tailscale on any network, unlike Wireless debugging
_serial_cache: tuple[float, str | None] | None = None
_last_reconnect = 0.0


NO_PHONE = "\x00none"  # _pick_serial: adb lists phones, but every network link is dead
PROBE_TIMEOUT = 1.5


def _is_net(serial: str) -> bool:
    """A network serial adb can probe (IP:port), not USB or an mDNS name."""
    return bool(re.fullmatch(r"[\d.]+:\d{2,5}|\[[\w:]+\]:\d{2,5}", serial))


def _alive(addr: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """The address still accepts TCP. adb keeps a dead Wi-Fi link listed as "device" for minutes after the
    phone leaves the network, and commands then hang until they time out: this catches it in ~1 s."""
    host, port = addr.rsplit(":", 1)
    try:
        socket.create_connection((host.strip("[]"), int(port)), timeout=timeout).close()
        return True
    except (OSError, ValueError):
        return False


def _drop(serial: str) -> None:
    _raw("disconnect", serial, timeout=5)


def _pick_serial() -> str | None:
    """With several connections (e.g. USB cable + wireless debugging of the same phone) adb refuses commands
    ('more than one device'): pick one. USB first (faster, steadier), else the first wireless one.
    Network links that no longer answer are disconnected first; if that leaves nothing, returns NO_PHONE.
    Cached for SERIAL_TTL seconds; dropped on any device error."""
    global _serial_cache
    now = time.monotonic()
    if _serial_cache and now - _serial_cache[0] < SERIAL_TTL:
        return _serial_cache[1]
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True, timeout=10).stdout.decode(errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    rows = [ln.split() for ln in out.splitlines()[1:] if len(ln.split()) >= 2]
    ready = []
    for serial, state, *_ in rows:
        if _is_net(serial) and (state == "offline" or (state == "device" and not _alive(serial))):
            _drop(serial)  # stale: the phone left that network
        elif state == "device":
            ready.append(serial)
    if not ready:
        pick = NO_PHONE if rows else None  # None: plain adb gives its own "no devices" error
    else:
        pick = None if len(ready) == 1 else next((s for s in ready if ":" not in s), ready[0])
    _serial_cache = (now, pick)
    return pick


def _read_last() -> dict:
    try:
        data = json.loads(LAST_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_last(address: str | None = None, remote: str | None = None, **extra) -> None:
    """Remember the last phone address (LAN/Wireless debugging), its Tailscale address (remote) and
    bookkeeping (remote_error, remote_try)."""
    data = _read_last()
    if address is not None:
        data["address"] = address
    if remote is not None:
        data["remote"] = remote
    data.update(extra)
    try:
        LAST_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_FILE.write_text(json.dumps(data))
        LAST_FILE.chmod(0o600)
    except OSError:
        pass


def _load_last() -> str:
    addr = str(_read_last().get("address", ""))
    return addr if ADDR_RE.match(addr) else ""


def _load_remote() -> str:
    addr = str(_read_last().get("remote", ""))
    return addr if ADDR_RE.match(addr) else ""


def _raw(*args: str, timeout: float = 15) -> str:
    try:
        p = subprocess.run(["adb", *args], capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (p.stdout + p.stderr).decode(errors="replace")


def _mdns_candidates(host: str) -> list[str]:
    """Wireless-debugging addresses the phone advertises on the LAN (the port changes after a reboot or a
    Wireless debugging toggle). Same host first. Over Tailscale there's no mDNS, so this may be empty."""
    found = re.findall(r"_adb-tls-connect\._tcp\.?\s+([\w.:\[\]-]+:\d{2,5})", _raw("mdns", "services", timeout=8))
    return sorted(set(found), key=lambda a: a.rsplit(":", 1)[0] != host)


def _tailscale_phone_ip() -> str:
    """Tailscale IPv4 of the phone: the first Android peer in `tailscale status`, online ones first."""
    try:
        p = subprocess.run(["tailscale", "status", "--json"], capture_output=True, timeout=5)
        peers = (json.loads(p.stdout or b"{}") or {}).get("Peer") or {}
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        return ""
    android = [x for x in peers.values() if isinstance(x, dict) and str(x.get("OS", "")).lower() == "android"]
    for peer in sorted(android, key=lambda x: not x.get("Online")):
        ip = next((i for i in peer.get("TailscaleIPs") or [] if re.fullmatch(r"[\d.]+", str(i))), "")
        if ip:
            return ip
    return ""


def _connected(out: str) -> bool:
    return bool(re.search(r"\b(connected|already connected) to\b", out)) and "failed" not in out


def _enable_remote(address: str) -> str:
    """Make the phone reachable off home Wi-Fi: Wireless debugging turns itself off on mobile data and its
    port changes, so switch adbd to the fixed TCP port REMOTE_PORT (stays until the phone reboots) and save
    the phone's Tailscale address. Returns that address, or "" (reason saved as remote_error)."""
    if not load_config().get("phone_remote_adb", True):
        _save_last(remote_error="Remote phone access is off (phone_remote_adb = false).")
        return ""
    _save_last(remote_try=time.time())
    ts = _tailscale_phone_ip()
    if not ts:
        _save_last(remote_error="Phone not found on Tailscale (is the Tailscale app on and signed in?).")
        return ""
    remote = f"{ts}:{REMOTE_PORT}"
    host, port = address.rsplit(":", 1)
    if port != str(REMOTE_PORT) and not _alive(remote):
        out = _raw("-s", address, "tcpip", str(REMOTE_PORT), timeout=15)
        if "restarting" not in out.lower():
            _save_last(remote_error=f"adb tcpip {REMOTE_PORT} failed: {out.strip()[-160:] or 'no output'}")
            return ""
        # Legacy `adb tcpip` has no encryption, so ROSE only ever talks to port 5555 through Tailscale (WireGuard),
        # never over the LAN address in clear, even at home.
        for _ in range(8):  # adbd needs a moment to come back on the new port
            time.sleep(1)
            if _connected(_raw("connect", remote)):
                _save_last(address=remote)
                break
        else:
            _raw("connect", address)  # keep the original link if 5555 didn't come up
            _save_last(remote_error=f"Port {REMOTE_PORT} didn't open on the phone after adb tcpip.")
            return ""
    _save_last(remote=remote, remote_error="")
    return remote


def remote_hint() -> str:
    """Why the saved Tailscale address isn't answering (for the dashboard / errors); "" if unknown or fine."""
    remote = _load_remote()
    if not remote:
        return ""
    host, port = remote.rsplit(":", 1)
    try:
        socket.create_connection((host, int(port)), timeout=2).close()
        return ""
    except ConnectionRefusedError:
        return (f"Phone is online on Tailscale but its ADB port {port} is closed (phone rebooted?). "
                "Reconnect once on home Wi-Fi to re-enable remote access.")
    except OSError:
        return "Phone not reachable on Tailscale: is it online with the Tailscale app connected?"


def _reconnect() -> bool:
    """Reconnect to the last phone: its saved address, the port it now advertises over mDNS (same LAN), else
    its Tailscale address (any network). Addresses that don't accept TCP are skipped, so a phone that left
    Wi-Fi costs ~1 s, not a 15 s `adb connect` hang."""
    global _last_reconnect, _serial_cache
    now = time.monotonic()
    last, remote = _load_last(), _load_remote()
    if not (last or remote) or now - _last_reconnect < RECONNECT_GAP:
        return False
    _last_reconnect = now
    _serial_cache = None
    candidates = []
    if last:
        host = last.rsplit(":", 1)[0]
        candidates = [last, *(a for a in _mdns_candidates(host) if a != last)]
    if not remote and last and (ts := _tailscale_phone_ip()):
        remote = f"{ts}:{REMOTE_PORT}"  # tcpip may be on from an earlier session without a saved remote
    if remote and remote not in candidates:
        candidates.append(remote)
    for addr in candidates:
        if _is_net(addr) and not _alive(addr):
            continue
        if _connected(_raw("connect", addr)):
            if addr == remote and remote != _load_remote():
                _save_last(remote=remote, remote_error="")
            elif addr not in (last, remote):
                _save_last(address=addr)
            return True
    return False


def _via(serial: str) -> str:
    if not _is_net(serial):
        return "usb"
    host = serial.rsplit(":", 1)[0]
    a, b = (int(x) for x in host.split(".")[:2]) if host.count(".") == 3 else (0, 0)
    return "tailscale" if a == 100 and 64 <= b < 128 else "wifi"


REMOTE_RETRY = 120  # seconds between automatic tries to switch on the Tailscale fallback


def link_status(reconnect: bool = False) -> dict:
    """How the phone is linked right now: via usb / wifi / tailscale / offline, plus the Tailscale fallback
    state. Dead network links are dropped first; reconnect=True also tries to bring the phone back."""
    global _serial_cache
    _serial_cache = None
    _pick_serial()  # drops stale links
    if reconnect and not _ready_serials():
        _reconnect()
    ready = _ready_serials()
    data = _read_last()
    remote = _load_remote()
    st = {"via": _via(ready[0]) if ready else "offline", "serial": ready[0] if ready else "",
          "last": _load_last(), "remote": remote, "remote_on": bool(remote) and _alive(remote),
          "remote_error": str(data.get("remote_error", ""))}
    if not ready:
        st["hint"] = remote_hint() or ("Phone not reachable. On Wi-Fi: turn on Wireless debugging. Off Wi-Fi: "
                                       "needs the Tailscale fallback, which is set up on the next Wi-Fi connect.")
    return st


SCAN_GAP = 120  # seconds between port scans while the phone is on the LAN but its port changed
_last_scan = 0.0


def _host_up(addr: str) -> bool:
    """The host answers at all: a refused connection means the phone is there and only the port moved."""
    host, port = addr.rsplit(":", 1)
    try:
        socket.create_connection((host, int(port)), timeout=PROBE_TIMEOUT).close()
        return True
    except ConnectionRefusedError:
        return True
    except (OSError, ValueError):
        return False


def _rescan_port() -> bool:
    """Wireless debugging got a new port and the phone doesn't announce it over mDNS: scan for it (the QR
    pairing scan), at most every SCAN_GAP s and only while the phone answers on the LAN."""
    global _last_scan
    last = _load_last()
    if not last or not _is_net(last) or time.monotonic() - _last_scan < SCAN_GAP or not _host_up(last):
        return False
    _last_scan = time.monotonic()
    from rose.phone_qr import find_port
    host, old = last.rsplit(":", 1)
    port = find_port(host, skip={int(old)})
    if port and _connected(_raw("connect", f"{host}:{port}")):
        _save_last(address=f"{host}:{port}")
        return True
    return False


def _ready_serials() -> list[str]:
    out = _raw("devices", timeout=10)
    return [ln.split()[0] for ln in out.splitlines()[1:] if ln.split()[1:2] == ["device"]]


def _prefer_lan(ts_serial: str) -> bool:
    """Back on home Wi-Fi: switch from the Tailscale link to the direct LAN one (lower latency for the mirror)."""
    last = _load_last()
    if not last or _via(last) != "wifi" or not _alive(last) or not _connected(_raw("connect", last)):
        return False
    _drop(ts_serial)
    return True


def keepalive() -> dict:
    """For rose-watch (every 10 s): drop dead links, reconnect, and switch on the Tailscale fallback once the
    phone is on Wi-Fi without it. Writes ROSE_HOME/phone_link.json for the dashboards."""
    if not (_load_last() or _load_remote()):
        return {"via": "offline", "never_connected": True}
    st = link_status(reconnect=True)
    if st["via"] == "offline" and _rescan_port():
        st = link_status()
    if st["via"] == "tailscale" and _prefer_lan(st["serial"]):
        st = link_status()
    if st["via"] == "wifi" and not st["remote_on"] and time.time() - float(_read_last().get("remote_try", 0)) > REMOTE_RETRY:
        _enable_remote(st["serial"])
        st = link_status()
    st["checked"] = time.time()
    try:
        (ROSE_HOME / "phone_link.json").write_text(json.dumps(st))
    except OSError:
        pass
    return st


DEVICE_GONE = ("no devices", "device offline", "not found")


def _gone(args: tuple, timeout: float, binary: bool, retry: bool):
    """The phone dropped: reconnect once and rerun, else raise a clear error."""
    global _serial_cache
    _serial_cache = None
    device_cmd = bool(args) and args[0] not in NO_DEVICE_CMDS
    if device_cmd and retry and _reconnect():
        return _adb(*args, timeout=timeout, binary=binary, _retry=False)
    hint = remote_hint() if device_cmd and retry else ""
    raise ToolError("No phone connected (tried reconnecting). " + (hint or "Check Wireless debugging is "
                    "on, then phone_connect with the IP:port it shows.")) from None


def _adb(*args: str, timeout: float = 20, binary: bool = False, _retry: bool = True) -> bytes | str:
    global _serial_cache
    require("adb", "sudo apt install adb")
    _ensure_server()
    device_cmd = bool(args) and args[0] not in NO_DEVICE_CMDS
    cmd = ["adb", *args]
    if device_cmd and (serial := _pick_serial()):
        if serial == NO_PHONE:
            return _gone(args, timeout, binary, _retry)
        cmd = ["adb", "-s", serial, *args]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        _serial_cache = None
        raise ToolError("The phone didn't answer in time (is it awake and still connected?).") from None
    if p.returncode != 0:
        err = (p.stderr or p.stdout).decode(errors="replace").strip()
        if any(s in err for s in DEVICE_GONE):
            return _gone(args, timeout, binary, _retry)
        raise ToolError(f"adb {args[0]} failed: {err[-300:] or 'no output'}")
    return p.stdout if binary else p.stdout.decode(errors="replace").strip()


def _shell(*args: str, timeout: float = 20) -> str:
    return _adb("shell", " ".join(shlex.quote(a) for a in args), timeout=timeout)


def phone_devices() -> list[dict]:
    """Phones connected over ADB (via usb / wifi / tailscale). Links that stopped answering are dropped."""
    global _serial_cache
    _serial_cache = None
    _pick_serial()
    out = _adb("devices", "-l")
    devs = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
            devs.append({"serial": parts[0], "state": parts[1], "model": info.get("model", ""),
                         "via": _via(parts[0])})
    return devs


def phone_connect(address: str, pairing_code: str = "", pairing_address: str = "") -> str:
    """Connect to the phone's wireless debugging (IP:port); first time also pairing_code + pairing_address."""
    address, pairing_address = address.strip(), (pairing_address or "").strip()
    if not ADDR_RE.match(address):
        raise ToolError("address must look like 100.102.41.115:37099 (IP:port from Wireless debugging).")
    if re.fullmatch(r"\d{2,5}", pairing_address):  # just the pairing port: same IP as the address
        pairing_address = f"{address.rsplit(':', 1)[0]}:{pairing_address}"
    if pairing_code:
        if not re.fullmatch(r"\d{6}", pairing_code) or not ADDR_RE.match(pairing_address or ""):
            raise ToolError("Pairing needs the 6-digit code and the pairing IP:port shown next to it.")
        _adb("pair", pairing_address, pairing_code, timeout=30)
    out = _adb("connect", address, timeout=20)
    if "connected" not in out or "failed" in out:
        raise ToolError(f"Couldn't connect: {out[-200:]}")
    _save_last(address=address)  # lets ROSE reconnect on its own after a drop
    remote = _enable_remote(address)
    if remote:
        return (f"Phone connected at {address}. Remote access on at {remote} over Tailscale "
                "(works off home Wi-Fi until the phone reboots).")
    why = str(_read_last().get("remote_error", ""))
    return f"Phone connected at {address}." + (f" Remote access (off Wi-Fi) not on: {why}" if why else "")


_jpeg_cap: bool | None = None  # does this phone's screencap support -j (JPEG)? learned on first use


def screencap(timeout: float = 30) -> bytes:
    """One phone screen capture as image bytes. `screencap -j` (JPEG, newer Android) is ~0.25 s over Wi-Fi vs
    ~1.6 s for `-p` (PNG encoding on the phone is the slow part); falls back to PNG where -j isn't supported."""
    global _jpeg_cap
    if _jpeg_cap is not False:
        data = _adb("exec-out", "screencap", "-j", binary=True, timeout=timeout)
        if data[:2] == b"\xff\xd8":
            _jpeg_cap = True
            return data
        _jpeg_cap = False
    return _adb("exec-out", "screencap", "-p", binary=True, timeout=timeout)


def phone_screenshot() -> list:
    """Phone screenshot (preview image + size). phone_tap/phone_swipe take coordinates in this preview."""
    global _last
    from mcp.server.mcpserver import Image
    from PIL import Image as PILImage

    png = screencap()
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


ADB_IME = "com.android.adbkeyboard/.AdbIME"


def _type_unicode(text: str) -> None:
    """`input text` can't do emoji, Hindi or '%'. ADBKeyboard (github.com/senzhk/ADBKeyBoard) can: switch to it,
    send the text base64-encoded, switch back to the user's keyboard."""
    if ADB_IME not in _shell("ime", "list", "-s"):
        raise ToolError("This text has emoji/non-English letters or '%', which plain ADB typing can't do. Install "
                        "ADBKeyboard on the phone (github.com/senzhk/ADBKeyBoard) once, then try again.")
    prev = _shell("settings", "get", "secure", "default_input_method").strip()
    _shell("ime", "enable", ADB_IME)
    _shell("ime", "set", ADB_IME)
    try:
        time.sleep(0.4)
        _shell("am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", base64.b64encode(text.encode()).decode())
        time.sleep(0.3)
    finally:
        if prev and prev != ADB_IME and prev != "null":
            _shell("ime", "set", prev)


def phone_type(text: str) -> str:
    """Type text into the focused field on the phone (no Enter). Emoji / non-English text need ADBKeyboard."""
    if not text or len(text) > 1000:
        raise ToolError("Text must be 1-1000 characters.")
    if "\n" in text:
        raise ToolError("No newlines: use phone_key('enter') (needs confirm) to submit.")
    if text.isascii() and text.isprintable() and "%" not in text:
        _shell("input", "text", text.replace(" ", "%s"))
    else:
        _type_unicode(text)
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
        packages = _packages()
        hits = [p for p in packages if q.replace(" ", "") in p.lower()]
        if not hits:
            raise ToolError(f"No phone app matches '{app}'.")
        if len(hits) > 1:
            exact = [p for p in hits if p.lower().split(".")[-1] == q.replace(" ", "")]
            if len(exact) != 1:
                raise ToolError(f"'{app}' matches several apps: {', '.join(hits[:8])}")
            hits = exact
        pkg = hits[0]
    if not PKG_RE.match(pkg):
        raise ToolError(f"Not a package id: {pkg}")
    # Resolve the launcher activity and start it in ONE adb round trip (~0.15 s). `monkey` boots a Java VM
    # (~1.2 s), so it's only the fallback when the resolver finds nothing.
    out = _adb("shell", f"c=$(cmd package resolve-activity --brief -a android.intent.action.MAIN "
                        f"-c android.intent.category.LAUNCHER {pkg} | tail -n 1); case \"$c\" in */*) "
                        f"am start -n \"$c\" -a android.intent.action.MAIN -c android.intent.category.LAUNCHER "
                        f"-f 0x10200000;; *) echo ROSE_NO_ACTIVITY;; esac")
    if "ROSE_NO_ACTIVITY" in out or "Error" in out:
        out = _shell("monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
        if "No activities found" in out or "Error" in out:
            raise ToolError(f"Couldn't launch {pkg}.")
    return f"Opened {pkg} on the phone."


_pkg_cache: tuple[float, list[str]] | None = None


def _packages() -> list[str]:
    """Installed package ids (cached 2 min: `pm list packages` is ~0.4 s and rarely changes)."""
    global _pkg_cache
    if _pkg_cache and time.monotonic() - _pkg_cache[0] < 120:
        return _pkg_cache[1]
    pkgs = [line.removeprefix("package:").strip() for line in _shell("pm", "list", "packages").splitlines()
            if line.startswith("package:")]
    if pkgs:
        _pkg_cache = (time.monotonic(), pkgs)
    return pkgs
