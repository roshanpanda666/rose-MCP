"""Find-my-phone over ADB: location, camera, microphone, sound, lock, reboot/power off, lost mode.

Works while the phone is on, has internet and ADB is reachable (Wi-Fi, or the Tailscale fallback on port 5555
that rose-watch switches on). Camera and microphone come from scrcpy-server (Apache-2.0, pushed to the phone
on first use, checksum-verified); Android shows its green camera/mic dot while they run, which can't be hidden.

Sound: unlocked, the clip plays at once (Files by Google preview). Locked, apps can't start playback, so the
clip becomes the ringtone of a one-off "ROSE" alarm (rings within 60 s) and the siren is a 1-second Clock
timer: both ring over the lock screen on the alarm stream.

Factory reset is not possible over ADB without root: use Google Find Hub (FIND_HUB) for a remote erase.
Every tool here is approval-gated for remote clients (guard.ALWAYS_APPROVE).
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import socket
import struct
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Literal

from rose.common import ToolError
from rose.config import ROSE_HOME
from rose.tools import phone

SCRCPY_VERSION = "5.0.1"
SCRCPY_SHA256 = "764eb6f79811d5211fe9df341120882ba9994c7a61b897d7bf3fb662e53bc536"
SCRCPY_URL = (f"https://github.com/Genymobile/scrcpy/releases/download/v{SCRCPY_VERSION}/"
              f"scrcpy-server-v{SCRCPY_VERSION}")
LOCAL_JAR = ROSE_HOME / "bin" / f"scrcpy-server-v{SCRCPY_VERSION}"
REMOTE_JAR = "/data/local/tmp/rose-scrcpy.jar"
OUT_DIR = ROSE_HOME / "phone"
PHONE_DIR = "/sdcard/Download/rose"
FIND_HUB = "https://www.google.com/android/find"
ALARM_LABEL = "ROSE"
PLAYERS = ("com.google.android.apps.nbu.files/.gateway.preview.AudioPreviewActivity",
           "com.google.android.apps.youtube.music/.audiopreview.AudioPreviewPlayerActivity")
PCM_RATE, PCM_CH = 48000, 2  # scrcpy audio_codec=raw: s16le, 48 kHz, stereo


# ---------------- scrcpy-server ----------------

def _local_jar() -> Path:
    """scrcpy-server on the laptop, downloaded once and checked against the release checksum."""
    if LOCAL_JAR.exists() and hashlib.sha256(LOCAL_JAR.read_bytes()).hexdigest() == SCRCPY_SHA256:
        return LOCAL_JAR
    LOCAL_JAR.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(SCRCPY_URL, timeout=60) as r:
            data = r.read()
    except OSError as e:
        raise ToolError(f"Couldn't download scrcpy-server ({e}).") from None
    if hashlib.sha256(data).hexdigest() != SCRCPY_SHA256:
        raise ToolError("Downloaded scrcpy-server failed its checksum; not using it.")
    tmp = LOCAL_JAR.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(LOCAL_JAR)
    return LOCAL_JAR


def _ensure_jar() -> None:
    """Push scrcpy-server to the phone unless the same file is already there."""
    local = _local_jar()
    size = str(phone._adb("shell", f"stat -c %s {REMOTE_JAR} 2>/dev/null || echo 0", timeout=15)).strip()
    if size != str(local.stat().st_size):
        phone._adb("push", str(local), REMOTE_JAR, timeout=180)


def _adb_base() -> list[str]:
    serial = phone._pick_serial()
    if serial == phone.NO_PHONE:
        raise ToolError("No phone connected.")
    return ["adb", *(["-s", serial] if serial else [])]


class Scrcpy:
    """One scrcpy-server stream: a phone camera (raw H.264) or the phone microphone (raw PCM s16le 48k stereo).
    Use as a context manager; read() returns the next chunk, b"" at the end."""

    def __init__(self, camera: str | None = None, mic: bool = False, max_size: int = 1280,
                 bit_rate: int = 4_000_000):
        if bool(camera) == bool(mic):
            raise ValueError("camera or mic")
        self.camera, self.mic, self.max_size, self.bit_rate = camera, mic, max_size, bit_rate
        self.proc: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self.port = ""
        self._first = b""

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def start(self) -> None:
        _ensure_jar()
        base = _adb_base()
        scid = f"{secrets.randbelow(0x7FFFFFFF):08x}"
        self.port = subprocess.run([*base, "forward", "tcp:0", f"localabstract:scrcpy_{scid}"],
                                   capture_output=True, text=True, timeout=15).stdout.strip()
        if not self.port.isdigit():
            raise ToolError("Couldn't open an ADB tunnel to the phone.")
        self._base = base
        opts = [f"scid={scid}", "log_level=warn", "tunnel_forward=true", "raw_stream=true", "control=false",
                "cleanup=false"]
        if self.camera:
            opts += ["video=true", "audio=false", "video_source=camera", f"camera_facing={self.camera}",
                     f"max_size={self.max_size}", f"video_bit_rate={self.bit_rate}"]
        else:
            opts += ["video=false", "audio=true", "audio_source=mic", "audio_codec=raw"]
        cmd = f"CLASSPATH={REMOTE_JAR} app_process / com.genymobile.scrcpy.Server {SCRCPY_VERSION} " + " ".join(opts)
        self.proc = subprocess.Popen([*base, "shell", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        end = time.time() + 15
        while time.time() < end:
            if self.proc.poll() is not None:
                break
            try:
                s = socket.create_connection(("127.0.0.1", int(self.port)), timeout=3)
            except OSError:
                time.sleep(0.2)
                continue
            s.settimeout(4)
            try:
                first = s.recv(65536)
            except socket.timeout:
                first = None  # connected, nothing sent yet
            except OSError:
                first = b""
            if first == b"":  # adb accepted, but the server isn't listening yet
                s.close()
                time.sleep(0.25)
                continue
            self.sock, self._first = s, first or b""
            s.settimeout(8)
            return
        out = b""
        if self.proc.poll() is not None and self.proc.stdout:
            out = self.proc.stdout.read(2000)
        self.stop()
        why = out.decode(errors="replace").strip()
        raise ToolError("The phone's " + ("camera" if self.camera else "microphone") + " didn't start"
                        + (f": {why[-300:]}" if why else " (phone locked down, or camera/mic in use?)."))

    def read(self) -> bytes:
        if self._first:
            data, self._first = self._first, b""
            return data
        if not self.sock:
            return b""
        try:
            return self.sock.recv(65536)
        except OSError:
            return b""

    def stop(self) -> None:
        if self.sock:
            try:
                self.sock.close()  # the server stops when its socket closes
            except OSError:
                pass
            self.sock = None
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        if self.port:
            subprocess.run([*getattr(self, "_base", ["adb"]), "forward", "--remove", f"tcp:{self.port}"],
                           capture_output=True, timeout=10)
            self.port = ""


KEYCODES = {"back": 4, "home": 3, "recents": 187, "enter": 66, "del": 67, "forward_del": 112, "tab": 61,
            "up": 19, "down": 20, "left": 21, "right": 22, "power": 26, "volume_up": 24, "volume_down": 25,
            "escape": 111, "move_home": 122, "move_end": 123, "page_up": 92, "page_down": 93}
AUD = b"\x00\x00\x00\x01\x09\xf0"  # H.264 access unit delimiter: tells the decoder a frame is complete


class ScrcpyScreen:
    """Live phone screen + input through scrcpy-server (what the scrcpy app does): H.264 packets with frame
    boundaries, so each frame decodes the moment it arrives, and a control socket that injects touches, keys
    and text in ~10 ms (vs ~200 ms for `adb shell input`). Coordinates are in the video's pixel space."""

    def __init__(self, max_size: int = 1100, bit_rate: int = 8_000_000, max_fps: int = 60):
        self.max_size, self.bit_rate, self.max_fps = max_size, bit_rate, max_fps
        self.proc: subprocess.Popen | None = None
        self.video: socket.socket | None = None
        self.control: socket.socket | None = None
        self.port = ""
        self.size = (0, 0)  # current video size (w, h); touches must use it
        self.lock = threading.Lock()
        self._base = ["adb"]

    def start(self) -> None:
        _ensure_jar()
        self._base = _adb_base()
        scid = f"{secrets.randbelow(0x7FFFFFFF):08x}"
        self.port = subprocess.run([*self._base, "forward", "tcp:0", f"localabstract:scrcpy_{scid}"],
                                   capture_output=True, text=True, timeout=15).stdout.strip()
        if not self.port.isdigit():
            raise ToolError("Couldn't open an ADB tunnel to the phone.")
        opts = (f"scid={scid} log_level=warn tunnel_forward=true cleanup=false audio=false control=true "
                f"video_source=display max_size={self.max_size} max_fps={self.max_fps} "
                f"video_bit_rate={self.bit_rate} send_device_meta=false send_frame_meta=true send_dummy_byte=true "
                "clipboard_autosync=false")
        self.proc = subprocess.Popen([*self._base, "shell", f"CLASSPATH={REMOTE_JAR} app_process / "
                                      f"com.genymobile.scrcpy.Server {SCRCPY_VERSION} {opts}"],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        end = time.time() + 15
        while time.time() < end and self.proc.poll() is None:
            try:
                s = socket.create_connection(("127.0.0.1", int(self.port)), timeout=3)
            except OSError:
                time.sleep(0.2)
                continue
            s.settimeout(5)
            try:
                dummy = s.recv(1)  # the server sends one byte once it accepted the video socket
            except OSError:
                dummy = b""
            if not dummy:
                s.close()
                time.sleep(0.25)
                continue
            self.video = s
            self.control = socket.create_connection(("127.0.0.1", int(self.port)), timeout=5)
            self.control.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(10)
            self._exact(4)  # codec id ("h264")
            return
        out = self.proc.stdout.read(2000).decode(errors="replace") if self.proc.poll() is not None else ""
        self.stop()
        raise ToolError("The phone's screen stream didn't start" + (f": {out.strip()[-300:]}" if out.strip() else "."))

    def _exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.video.recv(n - len(buf)) if self.video else b""
            if not chunk:
                raise EOFError
            buf += chunk
        return buf

    def packets(self):
        """Yield H.264 data ready for a decoder: config packets as they are, frames followed by an AUD.
        Ends when the stream stops."""
        try:
            while self.video:
                head = self._exact(12)
                if head[0] & 0x80:  # session packet: (new) video size, e.g. after rotation
                    self.size = struct.unpack(">II", head[4:])
                    continue
                flags, size = struct.unpack(">QI", head)
                data = self._exact(size)
                yield data if (flags >> 62) & 1 else data + AUD
        except (EOFError, OSError):
            return

    def _send(self, msg: bytes) -> None:
        if not self.control:
            raise ToolError("Phone screen control isn't running.")
        with self.lock:
            self.control.sendall(msg)

    def touch(self, action: Literal["down", "up", "move"], x: float, y: float) -> None:
        w, h = self.size
        act = {"down": 0, "up": 1, "move": 2}[action]
        x, y = max(0, min(int(x), w - 1)), max(0, min(int(y), h - 1))
        self._send(struct.pack(">BBqiiHHHii", 2, act, -2, x, y, w, h, 0 if act == 1 else 0xFFFF,
                               1, 0 if act == 1 else 1))

    def key(self, key: str | int) -> None:
        code = KEYCODES.get(str(key).lower(), key)
        code = int(code)
        for act in (0, 1):
            self._send(struct.pack(">BBiii", 0, act, code, 0, 0))

    def text(self, text: str) -> None:
        data = text.encode()[:300]
        self._send(struct.pack(">BI", 1, len(data)) + data)

    def swipe(self, x1: float, y1: float, x2: float, y2: float, ms: int = 200) -> None:
        steps = max(4, ms // 16)
        self.touch("down", x1, y1)
        for i in range(1, steps + 1):
            time.sleep(ms / 1000 / steps)
            self.touch("move", x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps)
        self.touch("up", x2, y2)

    def stop(self) -> None:
        for s in (self.video, self.control):
            if s:
                try:
                    s.close()
                except OSError:
                    pass
        self.video = self.control = None
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        if self.port:
            subprocess.run([*self._base, "forward", "--remove", f"tcp:{self.port}"], capture_output=True, timeout=10)
            self.port = ""


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _out(name: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR / name


# ---------------- location ----------------

LOC_RE = re.compile(r"(last location \(fine\)|^\s*(?:gps|network|fused)):\s*\{(\w+),\s*(-?\d+\.\d+),(-?\d+\.\d+)"
                    r"±([\d.]+)m.*?ert=(\d\d-\d\d \d\d:\d\d:\d\d)", re.M)


def _read_location() -> dict | None:
    out = str(phone._adb("shell", "dumpsys activity service com.google.android.gms 2>/dev/null | grep -E "
                         "'last location \\(fine\\)|^ +(gps|network|fused): \\{'; echo NOW $(date '+%Y %m-%d %H:%M:%S')",
                         timeout=40))
    now_m = re.search(r"NOW (\d{4}) (\d\d-\d\d \d\d:\d\d:\d\d)", out)
    best = None
    for m in LOC_RE.finditer(out):
        _, provider, lat, lon, acc, ert = m.groups()
        year = now_m.group(1) if now_m else str(datetime.now().year)
        when = datetime.strptime(f"{year}-{ert}", "%Y-%m-%d %H:%M:%S")
        cand = {"lat": float(lat), "lon": float(lon), "accuracy_m": float(acc), "provider": provider,
                "time": when.strftime("%Y-%m-%d %H:%M:%S")}
        if now_m:
            now = datetime.strptime(f"{now_m.group(1)}-{now_m.group(2)}", "%Y-%m-%d %H:%M:%S")
            cand["age_s"] = max(0, int((now - when).total_seconds()))
        key = (cand.get("age_s", 0) // 60, cand["accuracy_m"])  # newest minute first, then the most accurate
        if best is None or key < best[0]:
            best = (key, cand)
    return best[1] if best else None


def phone_location(refresh: bool = False) -> dict:
    """Where the phone is: last known lat/lon, accuracy, age and a Google Maps link. refresh=true briefly opens
    Google Maps on the phone (visible on its screen, unlocked phones only) to get a fresh fix."""
    enabled = str(phone._adb("shell", "cmd location is-location-enabled", timeout=15)).strip()
    if enabled == "false":
        phone._adb("shell", "cmd location set-location-enabled true", timeout=15)
    if refresh:
        phone._adb("shell", "am start -a android.intent.action.VIEW -d geo:0,0 -p com.google.android.apps.maps",
                   timeout=15)
        time.sleep(10)
        phone._adb("shell", "input keyevent KEYCODE_HOME", timeout=15)
    loc = _read_location() or (time.sleep(1.5) or _read_location())  # the gms dump is sometimes empty
    if not loc:
        raise ToolError("The phone has no location fix yet (location off or no Google Play Services).")
    loc["maps"] = f"https://maps.google.com/?q={loc['lat']},{loc['lon']}"
    if enabled == "false":
        loc["note"] = "Location was off; ROSE switched it on. Ask again in a minute for a fresh fix."
    return loc


# ---------------- camera ----------------

def _grab_jpeg(camera: str, settle: float = 1.8, max_size: int = 1600) -> bytes:
    """One photo: stream the camera for `settle` seconds (auto-exposure), keep the last decoded frame."""
    buf = bytearray()
    with Scrcpy(camera=camera, max_size=max_size, bit_rate=8_000_000) as s:
        end = time.time() + settle
        while time.time() < end:
            chunk = s.read()
            if not chunk:
                break
            buf += chunk
    if not buf:
        raise ToolError("The camera sent no picture.")
    p = subprocess.run(["ffmpeg", "-v", "error", "-f", "h264", "-i", "-", "-update", "1", "-q:v", "3",
                        "-f", "image2", "-c:v", "mjpeg", "pipe:1"], input=bytes(buf), capture_output=True, timeout=60)
    if not p.stdout:
        raise ToolError("Couldn't decode the camera picture: " + p.stderr.decode(errors="replace")[-200:])
    jpg = p.stdout
    last = jpg.rfind(b"\xff\xd8")  # several frames may come out: keep the last one
    return jpg[last:] if last > 0 else jpg


def phone_photo(camera: Literal["back", "front"] = "back") -> list:
    """Take a photo with the phone's back or front camera (no shutter sound; Android shows its camera dot).
    Saved under ~/rose/phone."""
    from mcp.server.mcpserver import Image
    jpg = _grab_jpeg(camera)
    path = _out(f"photo-{_stamp()}-{camera}.jpg")
    path.write_bytes(jpg)
    return [f"Phone {camera} camera photo saved to {path}.", Image(data=jpg, format="jpeg")]


# ---------------- microphone ----------------

def _pcm_level(pcm: bytes) -> float:
    """Loudness in dBFS of s16le audio (rough, for 'is anyone talking')."""
    import array
    import math
    a = array.array("h", pcm[: len(pcm) - len(pcm) % 2])
    if not a:
        return -120.0
    rms = math.sqrt(sum(x * x for x in a[:: max(1, len(a) // 200000)]) / len(a[:: max(1, len(a) // 200000)]))
    return round(20 * math.log10(rms / 32768), 1) if rms else -120.0


def phone_mic_record(seconds: int = 15) -> str:
    """Record the phone's microphone for N seconds (max 300) to an .ogg under ~/rose/phone (Android shows its
    mic dot)."""
    seconds = max(1, min(int(seconds), 300))
    need = seconds * PCM_RATE * PCM_CH * 2
    buf = bytearray()
    with Scrcpy(mic=True) as s:
        end = time.time() + seconds + 10
        while len(buf) < need and time.time() < end:
            chunk = s.read()
            if not chunk:
                break
            buf += chunk
    if not buf:
        raise ToolError("The phone's microphone sent no audio.")
    path = _out(f"mic-{_stamp()}.ogg")
    p = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(PCM_RATE), "-ac", str(PCM_CH),
                        "-i", "-", "-c:a", "libopus", "-b:a", "48k", str(path)], input=bytes(buf),
                       capture_output=True, timeout=120)
    if p.returncode:
        raise ToolError("Couldn't save the recording: " + p.stderr.decode(errors="replace")[-200:])
    got = len(buf) / (PCM_RATE * PCM_CH * 2)
    return f"Recorded {got:.0f} s from the phone's microphone to {path} (level {_pcm_level(bytes(buf))} dBFS)."


class MicListen:
    """Live phone microphone on the laptop speakers (pw-play), until stop()."""

    def __init__(self):
        self.s = Scrcpy(mic=True)
        self.player: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.level = -120.0
        self.running = False

    def start(self) -> None:
        self.s.start()
        self.player = subprocess.Popen(["pw-play", "--format", "s16", "--rate", str(PCM_RATE), "--channels",
                                        str(PCM_CH), "-"], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.running = True
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self) -> None:
        while self.running:
            chunk = self.s.read()
            if not chunk:
                break
            self.level = _pcm_level(chunk)
            try:
                self.player.stdin.write(chunk)
            except (OSError, ValueError):
                break
        self.running = False

    def stop(self) -> None:
        self.running = False
        self.s.stop()
        if self.player:
            try:
                self.player.stdin.close()
            except OSError:
                pass
            self.player.kill()


# ---------------- sound on the phone ----------------

def _tts(text: str) -> bytes:
    """Speech audio from the ROSE phone app's voice (Groq Orpheus, else Piper on the laptop)."""
    from rose.config import load_config
    env = Path(str(load_config().get("companion_env_file", "~/0x/projects/rose-phone-app/backend/.env"))).expanduser()
    token = ""
    try:
        for line in env.read_text().splitlines():
            if line.startswith("APP_TOKEN="):
                token = line[10:].strip().strip("'\"")
    except OSError:
        pass
    url = str(load_config().get("companion_url", "http://127.0.0.1:8790")).rstrip("/") + "/api/tts"
    req = urllib.request.Request(url, data=json.dumps({"text": text[:900]}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except OSError as e:
        raise ToolError(f"No voice available to speak the text ({e}). Record a clip instead.") from None


def _to_mp3(src: bytes | Path, repeat: int = 1) -> bytes:
    args = ["ffmpeg", "-v", "error"]
    if repeat > 1:
        args += ["-stream_loop", str(repeat - 1)]
    if isinstance(src, Path):
        args += ["-i", str(src)]
        inp = None
    else:
        args += ["-i", "pipe:0"]
        inp = src
    if repeat > 1 and inp is not None:  # -stream_loop needs a seekable file
        tmp = _out(f"tmp-{_stamp()}.audio")
        tmp.write_bytes(inp)
        try:
            return _to_mp3(tmp, repeat)
        finally:
            tmp.unlink(missing_ok=True)
    p = subprocess.run([*args, "-ac", "1", "-ar", "44100", "-b:a", "96k", "-f", "mp3", "pipe:1"], input=inp,
                       capture_output=True, timeout=120)
    if not p.stdout:
        raise ToolError("Couldn't convert the audio: " + p.stderr.decode(errors="replace")[-200:])
    return p.stdout


def _push_audio(mp3: bytes) -> str:
    """Put the clip on the phone and return its MediaStore content:// URI (apps and the Clock can read it)."""
    name = f"rose-{_stamp()}.mp3"
    local = _out(name)
    local.write_bytes(mp3)
    phone._adb("shell", f"mkdir -p {PHONE_DIR}", timeout=15)
    phone._adb("push", str(local), f"{PHONE_DIR}/{name}", timeout=120)
    phone._adb("shell", "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
               f"-d file://{PHONE_DIR}/{name}", timeout=15)
    for _ in range(12):
        out = str(phone._adb("shell", "content query --uri content://media/external/audio/media --projection _id "
                             f"--where \"_display_name='{name}'\"", timeout=15))
        m = re.search(r"_id=(\d+)", out)
        if m:
            return f"content://media/external/audio/media/{m.group(1)}"
        time.sleep(0.5)
    raise ToolError("The phone didn't index the audio clip.")


def _locked() -> bool:
    out = str(phone._adb("shell", "dumpsys window | grep -E 'mDreamingLockscreen|isKeyguardShowing|"
                         "mShowingLockscreen|KeyguardShowing' ; dumpsys power | grep -m1 mWakefulness=", timeout=20))
    if "mWakefulness=Asleep" in out or "mWakefulness=Dozing" in out:
        return True
    return bool(re.search(r"(KeyguardShowing|ShowingLockscreen|DreamingLockscreen)\W*[=:]\s*true", out, re.I))


def _set_volume(stream: int, percent: int) -> None:
    out = str(phone._adb("shell", f"cmd media_session volume --stream {stream} --get", timeout=15))
    m = re.search(r"range \[(\d+)\.\.(\d+)\]", out)
    top = int(m.group(2)) if m else 15
    phone._adb("shell", f"cmd media_session volume --stream {stream} --set {round(top * percent / 100)}",
               timeout=15)


def _alarm_with(uri: str) -> int:
    """One-off alarm at the next minute that rings `uri`; returns seconds until it rings."""
    now = time.time()
    at = time.localtime(now + 60)
    phone._adb("shell", "am start -a android.intent.action.SET_ALARM "
               f"--ei android.intent.extra.alarm.HOUR {at.tm_hour} --ei android.intent.extra.alarm.MINUTES {at.tm_min} "
               f"--es android.intent.extra.alarm.MESSAGE {ALARM_LABEL} --es android.intent.extra.alarm.RINGTONE {uri} "
               "--ez android.intent.extra.alarm.VIBRATE true --ez android.intent.extra.alarm.SKIP_UI true", timeout=20)
    return int(time.mktime(at) - at.tm_sec - now)


def play_on_phone(mp3: bytes, volume: int | None = 100) -> str:
    """Play a clip on the phone now (unlocked), or as a ROSE alarm within 60 s (locked)."""
    uri = _push_audio(mp3)
    if _locked():
        if volume is not None:
            _set_volume(4, volume)  # alarm stream
        secs = _alarm_with(uri)
        return f"Phone is locked: the message rings as a '{ALARM_LABEL}' alarm in about {secs} s (loops until dismissed)."
    if volume is not None:
        _set_volume(3, volume)  # music stream
    for comp in PLAYERS:
        out = str(phone._adb("shell", f"am start -n {comp} -a android.intent.action.VIEW -t audio/mpeg -d {uri} "
                             "--grant-read-uri-permission", timeout=20))
        if "Error" not in out:
            return "Playing on the phone now."
    phone._adb("shell", f"am start -a android.intent.action.VIEW -t audio/mpeg -d {uri} --grant-read-uri-permission",
               timeout=20)
    return "Opened the clip on the phone (it may ask which app to play it with)."


def phone_speak(text: str = "", audio_file: str = "", siren: bool = False, volume: int = 100,
                confirm: bool = False) -> str:
    """Make the phone speak or ring: text (spoken by Rose's voice), audio_file (a clip on the laptop), and/or
    siren (loud Clock timer, rings until dismissed). Locked phone: the message rings as an alarm within 60 s.
    Needs confirm=true."""
    if not confirm:
        raise ToolError("This makes the phone play sound out loud. If the user asked for it, call again with confirm=true.")
    if not (text or audio_file or siren):
        raise ToolError("Give text, audio_file, or siren=true.")
    volume = max(0, min(int(volume), 100))
    done = []
    if siren:
        _set_volume(4, volume)
        phone._adb("shell", "am start -a android.intent.action.SET_TIMER --ei android.intent.extra.alarm.LENGTH 1 "
                   "--es android.intent.extra.alarm.MESSAGE ROSE --ez android.intent.extra.alarm.SKIP_UI true",
                   timeout=20)
        done.append("Siren (timer) ringing on the phone until someone dismisses it.")
    if text or audio_file:
        if audio_file:
            path = Path(audio_file).expanduser()
            if not path.is_file():
                raise ToolError(f"No such audio file: {audio_file}")
            mp3 = _to_mp3(path)
        else:
            mp3 = _to_mp3(_tts(text), repeat=1)
        done.append(play_on_phone(mp3, volume))
    return " ".join(done)


def phone_stop_sound() -> str:
    """Stop ROSE's sound on the phone: dismiss the siren timer and the ROSE alarm, stop playback."""
    for cmd in ("am start -a android.intent.action.DISMISS_TIMER",
                "am start -a android.intent.action.DISMISS_ALARM --es android.intent.extra.alarm.SEARCH_MODE "
                f"android.label --es android.intent.extra.alarm.MESSAGE {ALARM_LABEL}",
                "input keyevent KEYCODE_MEDIA_STOP"):
        try:
            phone._adb("shell", cmd, timeout=15)
        except ToolError:
            pass
    return "Stopped the siren / alarm / playback on the phone."


# ---------------- power ----------------

def phone_power(action: Literal["reboot", "poweroff"], confirm: bool = False) -> str:
    """Reboot or power off the phone. WARNING: afterwards ROSE loses the phone (the Tailscale ADB port resets) until
    it is back on Wi-Fi with Wireless debugging on, and power off also stops Google Find Hub tracking. Needs
    confirm=true."""
    if not confirm:
        raise ToolError(f"{action} cuts ROSE off from the phone until it's reconnected on Wi-Fi"
                        + (" and stops Google Find Hub tracking" if action == "poweroff" else "")
                        + ". If the user asked for it, call again with confirm=true.")
    if action == "reboot":
        phone._adb("reboot", timeout=20)
        return "Phone is rebooting. ROSE reconnects once it's on Wi-Fi with Wireless debugging on."
    out = ""
    for cmd in ("reboot -p", "svc power shutdown", "cmd power shutdown"):
        try:
            out = str(phone._adb("shell", cmd, timeout=20))
        except ToolError as e:
            out = str(e)
            if "No phone connected" in out:
                return "Phone is powering off."
            continue
        time.sleep(4)
        if phone.link_status()["via"] == "offline":
            return "Phone is powering off."
    raise ToolError(f"The phone refused to power off over ADB ({out[-160:]}). Use {FIND_HUB} instead.")


# ---------------- lost mode ----------------

def phone_lost_mode(message: str = "", siren: bool = True, confirm: bool = False) -> list:
    """Lost/stolen phone: location, a front-camera photo, then max volume, a siren and/or spoken message, then lock
    the screen. Alerts the laptop. Needs confirm=true. (Remote erase: Google Find Hub.)"""
    if not confirm:
        raise ToolError("Lost mode makes the phone ring loudly and locks it. If the user asked for it, call again "
                        "with confirm=true.")
    from mcp.server.mcpserver import Image
    from rose import push
    report, image = [], None
    try:
        loc = phone_location()
        report.append(f"Location: {loc['maps']} (±{loc['accuracy_m']:.0f} m, {loc.get('age_s', '?')} s old)")
    except ToolError as e:
        loc = None
        report.append(f"Location: failed ({e})")
    try:
        jpg = _grab_jpeg("front")
        path = _out(f"lost-{_stamp()}-front.jpg")
        path.write_bytes(jpg)
        image = Image(data=jpg, format="jpeg")
        report.append(f"Front photo: {path}")
    except ToolError as e:
        report.append(f"Front photo: failed ({e})")
    try:
        report.append(phone_speak(text=message, siren=siren, volume=100, confirm=True)
                      if (message or siren) else "No sound.")
    except ToolError as e:
        report.append(f"Sound: failed ({e})")
    try:
        phone._adb("shell", "input keyevent KEYCODE_SLEEP", timeout=15)
        report.append("Screen locked.")
    except ToolError as e:
        report.append(f"Lock: failed ({e})")
    report.append(f"Remote erase / more: {FIND_HUB}")
    push.push("Phone lost mode on", "\n".join(report), url=loc["maps"] if loc else FIND_HUB, priority="high")
    text = "Lost mode:\n" + "\n".join(report)
    return [text, image] if image else [text]
