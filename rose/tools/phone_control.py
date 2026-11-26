"""Phone device controls over ADB: volume, media, quick settings, status, screen, notifications, calls in
progress, alarms/timers, URLs and scrolling. Add-on to rose/tools/phone.py and phone_actions.py (those stay
as they are); everything here uses Android's own shell commands, no pixel guessing:

* volume: `cmd media_session volume --stream N --get/--set` (Android 10+), key events as a fallback;
* settings: `cmd wifi`, `cmd bluetooth_manager`, `svc data`, `cmd connectivity airplane-mode`,
  `cmd notification set_dnd`, `cmd location`, `cmd power set-mode` and `settings put` (brightness, rotation,
  screen timeout); the flashlight has no shell command, so its quick-settings tile is tapped by label;
* status: one `adb shell` round trip with dumpsys/settings reads.

Anything that can cut the phone off from ROSE (Wi-Fi off, airplane on, mobile data off: wireless ADB runs
over the network), dismiss notifications or answer/end a call is approval-worthy; the network ones need
confirm=true.
"""

from __future__ import annotations

import re
import time
from typing import Literal

from rose.common import ToolError
from rose.tools import phone

STREAMS = {"call": 0, "system": 1, "ring": 2, "media": 3, "alarm": 4, "notification": 5}
_VOL = re.compile(r"volume is (\d+) in range \[(\d+)\.\.(\d+)\]")
_premute: dict[str, int] = {}


# ---------------- volume ----------------

def _vol_get(stream: str) -> tuple[int, int, int]:
    """(current, min, max) for a stream."""
    out = phone._shell("cmd", "media_session", "volume", "--stream", str(STREAMS[stream]), "--get")
    m = _VOL.search(out)
    if not m:
        raise ToolError("This phone doesn't report volume levels (needs Android 10+).")
    return int(m[1]), int(m[2]), int(m[3])


def _vol_set(stream: str, value: int) -> None:
    phone._shell("cmd", "media_session", "volume", "--show", "--stream", str(STREAMS[stream]), "--set", str(value))


def _vol_text(stream: str, cur: int, lo: int, hi: int) -> str:
    pct = round(100 * (cur - lo) / max(1, hi - lo))
    return f"Phone {stream} volume {cur}/{hi} ({pct}%)" + (" - muted." if cur == lo else ".")


def phone_volume(action: Literal["get", "up", "down", "set", "mute", "unmute"] = "get",
                 stream: Literal["media", "ring", "alarm", "call", "notification", "system"] = "media",
                 level: int | None = None, steps: int = 1) -> str:
    """Phone volume: get, up/down (steps), set (level 0-100 %), mute, unmute. stream: media (default), ring,
    alarm, call, notification, system."""
    steps = max(1, min(int(steps), 15))
    try:
        cur, lo, hi = _vol_get(stream)
    except ToolError:
        if action in ("up", "down") and stream == "media":  # old Android: plain key presses
            for _ in range(steps):
                phone._shell("input", "keyevent", "KEYCODE_VOLUME_UP" if action == "up" else "KEYCODE_VOLUME_DOWN")
            return f"Pressed volume {action} {steps}x on the phone."
        raise
    if action == "get":
        return _vol_text(stream, cur, lo, hi)
    if action in ("up", "down"):
        new = cur + steps if action == "up" else cur - steps
    elif action == "set":
        if level is None or not 0 <= level <= 100:
            raise ToolError("set needs level 0-100 (percent).")
        new = lo + round((hi - lo) * level / 100)
    elif action == "mute":
        if cur > lo:
            _premute[stream] = cur
        new = lo
    else:  # unmute
        new = _premute.pop(stream, 0) or (cur if cur > lo else lo + max(1, (hi - lo) // 3))
    new = max(lo, min(hi, new))
    _vol_set(stream, new)
    return _vol_text(stream, new, lo, hi)


# ---------------- media ----------------

MEDIA_KEYS = {"play_pause": "KEYCODE_MEDIA_PLAY_PAUSE", "play": "KEYCODE_MEDIA_PLAY",
              "pause": "KEYCODE_MEDIA_PAUSE", "next": "KEYCODE_MEDIA_NEXT", "previous": "KEYCODE_MEDIA_PREVIOUS",
              "stop": "KEYCODE_MEDIA_STOP", "forward": "KEYCODE_MEDIA_FAST_FORWARD",
              "rewind": "KEYCODE_MEDIA_REWIND"}


def phone_media(action: Literal["play_pause", "play", "pause", "next", "previous", "stop", "forward",
                                "rewind"]) -> str:
    """Control whatever is playing on the phone (Spotify, YouTube, ...)."""
    phone._shell("input", "keyevent", MEDIA_KEYS[action])
    return f"Sent {action.replace('_', '/')} to the phone's media player."


# ---------------- quick settings ----------------

SETTINGS = Literal["wifi", "bluetooth", "mobile_data", "airplane", "dnd", "location", "battery_saver",
                   "auto_rotate", "brightness", "screen_timeout", "flashlight"]
_ON = {"on", "true", "1", "enable", "enabled", "yes"}
_OFF = {"off", "false", "0", "disable", "disabled", "no"}


def _onoff(value: str) -> bool:
    v = value.strip().lower()
    if v in _ON:
        return True
    if v in _OFF:
        return False
    raise ToolError("value must be 'on' or 'off'.")


def _run(*args: str) -> str:
    out = phone._shell(*args)
    if re.search(r"\b(Exception|Error|Unknown command|not allowed|Permission Denial)\b", out):
        raise ToolError(f"The phone refused: {out.strip()[-200:]}")
    return out


def _flashlight() -> str:
    from rose.tools import phone_actions as A
    phone._shell("cmd", "statusbar", "expand-settings")
    time.sleep(0.5)  # let the panel finish opening: a dump mid-animation misses the tile and costs another ~2.5 s
    try:
        tile = A._wait_ui(lambda ns: next((n for n in ns if re.search(r"flash ?light|torch", n["text"] + " " + n["desc"], re.I)), None), 3)
        if not tile:
            raise ToolError("No flashlight tile in the phone's quick settings.")
        A._tap(tile)
    finally:
        time.sleep(0.4)
        phone._shell("cmd", "statusbar", "collapse")
    return "Toggled the phone's flashlight."


def phone_setting(setting: SETTINGS, value: str = "on", confirm: bool = False) -> str:
    """Phone setting: value 'on'/'off' (wifi, bluetooth, mobile_data, airplane, dnd, location, battery_saver,
    auto_rotate); brightness 0-100 or 'auto'; screen_timeout seconds; flashlight toggles. Wi-Fi/data off,
    airplane on need confirm=true."""
    if setting == "flashlight":
        return _flashlight()
    if setting == "brightness":
        v = value.strip().lower()
        if v == "auto":
            _run("settings", "put", "system", "screen_brightness_mode", "1")
            return "Phone brightness set to automatic."
        try:
            pct = float(v.rstrip("%"))
        except ValueError:
            raise ToolError("brightness takes 0-100 or 'auto'.") from None
        if not 0 <= pct <= 100:
            raise ToolError("brightness takes 0-100 or 'auto'.")
        _run("settings", "put", "system", "screen_brightness_mode", "0")
        _run("settings", "put", "system", "screen_brightness", str(max(1, round(pct * 2.55))))
        return f"Phone brightness set to {pct:g}%."
    if setting == "screen_timeout":
        try:
            secs = int(float(value))
        except ValueError:
            raise ToolError("screen_timeout takes seconds (15-1800).") from None
        secs = max(15, min(secs, 1800))
        _run("settings", "put", "system", "screen_off_timeout", str(secs * 1000))
        return f"Phone screen timeout set to {secs} s."

    on = _onoff(value)
    cuts = (setting == "wifi" and not on) or (setting == "airplane" and on) or (setting == "mobile_data" and not on)
    if cuts and not confirm:
        raise ToolError(f"Turning {setting.replace('_', ' ')} {'on' if on else 'off'} can disconnect ROSE from the "
                        "phone (wireless ADB runs over the network). Ask the user, then call with confirm=true.")
    if setting == "wifi":
        try:
            _run("cmd", "wifi", "set-wifi-enabled", "enabled" if on else "disabled")
        except ToolError:
            _run("svc", "wifi", "enable" if on else "disable")
    elif setting == "bluetooth":
        try:
            _run("cmd", "bluetooth_manager", "enable" if on else "disable")
        except ToolError:
            _run("svc", "bluetooth", "enable" if on else "disable")
    elif setting == "mobile_data":
        _run("svc", "data", "enable" if on else "disable")
    elif setting == "airplane":
        _run("cmd", "connectivity", "airplane-mode", "enable" if on else "disable")
    elif setting == "dnd":
        _run("cmd", "notification", "set_dnd", "priority" if on else "off")
    elif setting == "location":
        _run("cmd", "location", "set-location-enabled", "true" if on else "false")
    elif setting == "battery_saver":
        _run("cmd", "power", "set-mode", "1" if on else "0")
    elif setting == "auto_rotate":
        _run("settings", "put", "system", "accelerometer_rotation", "1" if on else "0")
    name = {"dnd": "Do Not Disturb", "wifi": "Wi-Fi"}.get(setting, setting.replace("_", " "))
    return f"Phone {name} turned {'on' if on else 'off'}."


# ---------------- status ----------------

_STATUS_SCRIPT = "; ".join([
    "echo @@battery", "dumpsys battery",
    "echo @@power", "dumpsys power | grep -m1 mWakefulness=",
    "echo @@lock", "dumpsys window | grep -m2 -iE 'isKeyguardShowing|mShowingLockscreen'",
    "echo @@focus", "dumpsys window | grep -m1 mCurrentFocus",
    "echo @@wifi", "dumpsys wifi | grep -m1 'mWifiInfo SSID'",
    "echo @@call", "dumpsys telephony.registry | grep -m1 mCallState",
    *[f"echo @@s_{k}; settings get {ns} {k}" for ns, k in (
        ("global", "wifi_on"), ("global", "bluetooth_on"), ("global", "airplane_mode_on"),
        ("global", "mobile_data"), ("global", "zen_mode"), ("global", "low_power"),
        ("system", "screen_brightness"), ("system", "screen_brightness_mode"),
        ("system", "accelerometer_rotation"))],
    *[f"echo @@v_{n}; cmd media_session volume --stream {STREAMS[n]} --get" for n in ("media", "ring", "alarm")],
    "echo @@storage", "df -h /data | tail -1",
])


def _sections(out: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    for chunk in out.split("@@")[1:]:
        name, _, body = chunk.partition("\n")
        parts[name.strip()] = body.strip()
    return parts


def phone_status() -> dict:
    """Phone battery, screen/lock, foreground app, Wi-Fi network, call state, volumes, toggles, storage."""
    s = _sections(phone._adb("shell", _STATUS_SCRIPT, timeout=25))
    bat = dict(re.findall(r"^\s*([\w ]+?): (.+)$", s.get("battery", ""), re.M))
    plugged = [k.split()[0] for k in ("AC powered", "USB powered", "Wireless powered") if bat.get(k) == "true"]
    st = {"2": "charging", "3": "discharging", "4": "not charging", "5": "full"}.get(bat.get("status", ""), "unknown")
    out: dict = {"battery": {"level": int(bat["level"]) if bat.get("level", "").isdigit() else None, "state": st,
                             "plugged": plugged[0].lower() if plugged else None,
                             "temp_c": int(bat["temperature"]) / 10 if bat.get("temperature", "").isdigit() else None}}
    out["screen_on"] = "Awake" in s.get("power", "")
    out["locked"] = bool(re.search(r"(isKeyguardShowing|mShowingLockscreen)=true", s.get("lock", "")))
    m = re.search(r"\s([\w.]+)/", s.get("focus", ""))
    out["foreground_app"] = m[1] if m else None
    m = re.search(r'SSID: "?([^",]*)"?,', s.get("wifi", ""))
    out["wifi_network"] = m[1] if m and m[1] not in ("<unknown ssid>", "") else None
    m = re.search(r"mCallState=(\d)", s.get("call", ""))
    out["call"] = {"0": "idle", "1": "ringing", "2": "in call"}.get(m[1]) if m else None

    def flag(k):
        v = s.get(f"s_{k}", "")
        return None if not v.lstrip("-").isdigit() else v != "0"
    out.update(wifi=flag("wifi_on"), bluetooth=flag("bluetooth_on"), airplane=flag("airplane_mode_on"),
               mobile_data=flag("mobile_data"), dnd=flag("zen_mode"), battery_saver=flag("low_power"),
               auto_rotate=flag("accelerometer_rotation"))
    b = s.get("s_screen_brightness", "")
    out["brightness"] = "auto" if s.get("s_screen_brightness_mode") == "1" else (
        f"{round(int(b) / 2.55)}%" if b.isdigit() else None)
    vols = {}
    for n in ("media", "ring", "alarm"):
        m = _VOL.search(s.get(f"v_{n}", ""))
        if m:
            vols[n] = f"{m[1]}/{m[3]}"
    out["volume"] = vols or None
    df = s.get("storage", "").split()
    out["storage"] = {"size": df[1], "used": df[2], "free": df[3]} if len(df) >= 4 else None
    return out


# ---------------- screen ----------------

def _locked() -> bool:
    out = phone._adb("shell", "dumpsys window | grep -iE 'isKeyguardShowing|mShowingLockscreen'")
    return bool(re.search(r"(isKeyguardShowing|mShowingLockscreen)=true", out))


def phone_screen(action: Literal["wake", "lock", "unlock"]) -> str:
    """Phone screen: wake it, lock it (screen off), or unlock (only swipe/no-PIN locks; never enters a PIN)."""
    if action == "lock":
        phone._shell("input", "keyevent", "KEYCODE_SLEEP")
        return "Phone screen locked."
    phone._shell("input", "keyevent", "KEYCODE_WAKEUP")
    if action == "wake":
        return "Phone screen is on."
    phone._shell("wm", "dismiss-keyguard")
    time.sleep(0.8)
    if _locked():
        return "The phone has a PIN/pattern/fingerprint lock: ROSE woke it, but the user has to unlock it."
    return "Phone unlocked."


# ---------------- notifications ----------------

_NOTIF_FIELD = re.compile(r"android\.(title|text|bigText)=\w+ \((.*)\)\s*$")


def _notifications() -> list[dict]:
    out = phone._adb("shell", "dumpsys notification --noredact", timeout=25)
    items: list[dict] = []
    cur: dict | None = None
    for line in out.splitlines():
        m = re.search(r"NotificationRecord\(0x[0-9a-f]+: pkg=([\w.]+)", line)
        if m:
            cur = {"app": m[1], "title": "", "text": ""}
            items.append(cur)
            continue
        if cur is not None and (m := _NOTIF_FIELD.search(line)):
            key = "text" if m[1] == "bigText" and not cur["text"] else m[1]
            if key in ("title", "text") and not cur[key]:
                cur[key] = m[2][:200]
    seen, uniq = set(), []
    for n in items:
        k = (n["app"], n["title"], n["text"])
        if (n["title"] or n["text"]) and k not in seen and n["app"] != "android":
            seen.add(k)
            uniq.append(n)
    return uniq[:40]


def phone_notifications(action: Literal["list", "open", "quick_settings", "close", "clear_all"] = "list",
                        confirm: bool = False) -> list[dict] | str:
    """Phone notifications: list (app, title, text), open the shade, open quick settings, close it, or
    clear_all (dismisses everything: needs confirm=true)."""
    if action == "list":
        return _notifications() or "No notifications on the phone."
    if action == "open":
        phone._shell("cmd", "statusbar", "expand-notifications")
        return "Opened the phone's notification shade."
    if action == "quick_settings":
        phone._shell("cmd", "statusbar", "expand-settings")
        return "Opened the phone's quick settings."
    if action == "close":
        phone._shell("cmd", "statusbar", "collapse")
        return "Closed the phone's notification shade."
    if not confirm:
        raise ToolError("clear_all dismisses every notification on the phone. Ask the user, then confirm=true.")
    from rose.tools import phone_actions as A
    phone._shell("cmd", "statusbar", "expand-notifications")
    btn = A._wait_ui(lambda ns: next((n for n in ns if re.fullmatch(r"(clear|dismiss)( all)?",
                                                                       (n["text"] or n["desc"]).strip(), re.I)), None), 3)
    if not btn:
        phone._shell("cmd", "statusbar", "collapse")
        raise ToolError("No 'Clear all' button in the phone's notification shade (nothing clearable?).")
    A._tap(btn)
    time.sleep(0.4)
    phone._shell("cmd", "statusbar", "collapse")
    return "Cleared the phone's notifications."


# ---------------- calls in progress ----------------

def _call_state() -> str:
    m = re.search(r"mCallState=(\d)", phone._adb("shell", "dumpsys telephony.registry | grep -m1 mCallState"))
    return {"0": "idle", "1": "ringing", "2": "in call"}.get(m[1] if m else "", "unknown")


def phone_call_control(action: Literal["status", "answer", "end", "reject", "mute_mic", "speaker"]) -> str:
    """Handle the current phone call: status, answer (ringing), end/reject, mute_mic (toggles), speaker
    (toggles, taps the in-call Speaker button)."""
    state = _call_state()
    if action == "status":
        return f"Phone call state: {state}."
    if action == "answer":
        if state != "ringing":
            raise ToolError(f"No incoming call to answer (state: {state}).")
        phone._shell("input", "keyevent", "KEYCODE_CALL")
        return "Answered the call on the phone."
    if action in ("end", "reject"):
        if state == "idle":
            raise ToolError("No call to end on the phone.")  # ENDCALL while idle would just turn the screen off
        phone._shell("input", "keyevent", "KEYCODE_ENDCALL")
        return "Rejected the incoming call." if state == "ringing" else "Ended the call on the phone."
    if state != "in call":
        raise ToolError(f"No call in progress (state: {state}).")
    if action == "mute_mic":
        phone._shell("input", "keyevent", "KEYCODE_MUTE")
        return "Toggled the phone's microphone mute."
    from rose.tools import phone_actions as A
    btn = A._wait_ui(lambda ns: next((n for n in ns if re.search(r"speaker", n["text"] + " " + n["desc"], re.I)), None), 3)
    if not btn:
        raise ToolError("No Speaker button on the phone's call screen (open the call screen first).")
    A._tap(btn)
    return "Toggled the phone's speaker."


# ---------------- alarms / timers ----------------

def _parse_time(t: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?\s*", t.lower())
    if not m:
        raise ToolError("time must look like 07:30 (24-hour) or 7:30 pm.")
    h, mi = int(m[1]), int(m[2] or 0)
    if m[3]:
        if not 1 <= h <= 12:
            raise ToolError("With am/pm the hour is 1-12.")
        h = h % 12 + (12 if m[3] == "pm" else 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ToolError("Invalid time.")
    return h, mi


def phone_alarm(time: str = "", timer_minutes: float = 0, label: str = "") -> str:
    """Set an alarm on the phone (time '07:30' or '7:30 pm') or a countdown timer (timer_minutes), with an
    optional label. Neither given: opens the alarm list."""
    label = label.strip()[:60]
    extra = ["--es", "android.intent.extra.alarm.MESSAGE", label] if label else []
    if timer_minutes:
        secs = round(float(timer_minutes) * 60)
        if not 1 <= secs <= 24 * 3600:
            raise ToolError("timer_minutes must be between 1 second and 24 hours.")
        _run("am", "start", "-a", "android.intent.action.SET_TIMER", "--ei", "android.intent.extra.alarm.LENGTH",
             str(secs), "--ez", "android.intent.extra.alarm.SKIP_UI", "true", *extra)
        mins, s = divmod(secs, 60)
        return f"Timer set on the phone for {mins} min" + (f" {s} s" if s else "") + (f" ({label})." if label else ".")
    if not time.strip():
        _run("am", "start", "-a", "android.intent.action.SHOW_ALARMS")
        return "Opened the phone's alarms."
    h, mi = _parse_time(time)
    _run("am", "start", "-a", "android.intent.action.SET_ALARM", "--ei", "android.intent.extra.alarm.HOUR", str(h),
         "--ei", "android.intent.extra.alarm.MINUTES", str(mi), "--ez", "android.intent.extra.alarm.SKIP_UI", "true",
         *extra)
    return f"Alarm set on the phone for {h:02d}:{mi:02d}" + (f" ({label})." if label else ".")


# ---------------- URLs / scrolling ----------------

def phone_open_url(url: str) -> str:
    """Open an http(s) link on the phone (its default browser or the matching app, e.g. YouTube, Maps)."""
    u = url.strip()
    if not re.fullmatch(r"https?://[^\s'\"<>]{3,2000}", u):
        raise ToolError("Give a full http(s):// link.")
    _run("am", "start", "-a", "android.intent.action.VIEW", "-d", u)
    return f"Opened {u[:120]} on the phone."


def _screen_size() -> tuple[int, int]:
    out = phone._shell("wm", "size")
    sizes = re.findall(r"(\d+)x(\d+)", out)
    if not sizes:
        raise ToolError("Couldn't read the phone's screen size.")
    w, h = sizes[-1]  # "Override size" (if any) comes after "Physical size"
    return int(w), int(h)


def phone_scroll(direction: Literal["down", "up", "left", "right"] = "down", amount: float = 0.5) -> str:
    """Scroll the phone screen (down = see content further down). amount = fraction of the screen (0.1-0.9)."""
    w, h = _screen_size()
    a = max(0.1, min(float(amount), 0.9))
    cx, cy = w // 2, h // 2
    dy, dx = round(h * a / 2), round(w * a / 2)
    x1, y1, x2, y2 = {"down": (cx, cy + dy, cx, cy - dy), "up": (cx, cy - dy, cx, cy + dy),
                      "right": (cx + dx, cy, cx - dx, cy), "left": (cx - dx, cy, cx + dx, cy)}[direction]
    phone._shell("input", "swipe", str(x1), str(y1), str(x2), str(y2), "300")
    return f"Scrolled the phone {direction}."
