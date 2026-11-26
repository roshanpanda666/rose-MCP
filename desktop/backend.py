"""ROSE Desktop backend: JSON lines over stdin/stdout for the Electron window.

Runs under the installed ROSE tool's Python (~/.local/share/uv/tools/rose/bin/python) and calls ROSE's own
modules in-process, like the CustomTkinter dashboard does. No network port is opened.

Request:  {"id": 1, "method": "snapshot", "params": {}}
Response: {"id": 1, "result": ...}  or  {"id": 1, "error": "message"}
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

from rose import guard, health, tunnel
from rose.config import CONFIG_PATH, LOG_DIR, ROSE_HOME, load_config, update_settings

_out_lock = threading.Lock()
_probe = {"t": 0.0, "code": None}
_frame_lock = threading.Lock()


def _systemctl(*args: str) -> tuple[bool, str]:
    r = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def _detach(*cmd: str) -> None:
    subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------- overview / remote ----------------

def snapshot(force_probe: bool = False) -> dict:
    probe = force_probe or time.time() - _probe["t"] > 30
    s = health.collect(probe_public=probe)
    if probe:
        _probe.update(t=time.time(), code=s["public_probe"])
    else:
        s["public_probe"] = _probe["code"]
        s["alerts"] = health.alerts(s)
    s["tunnel"] = {"permanent": tunnel.is_permanent(), "provider": tunnel.provider(),
                   "readiness": [{"step": a, "ok": b, "fix": c} for a, b, c in tunnel.readiness()]}
    cfg = load_config()
    s["tunnel"]["ngrok_domain"] = cfg.get("ngrok_domain", "")
    s["calls_today_n"] = len(s.pop("calls_today"))
    s["remote_calls_today_n"] = len(s.pop("remote_calls_today"))
    s["grant"] = s.pop("gui_grant", None)
    s["config_path"] = str(CONFIG_PATH)
    s["face_running"] = subprocess.run(["pgrep", "-f", "[r]ose face"], capture_output=True).returncode == 0
    return s


def _remote_unit(svc: dict) -> str:
    for unit in ("rose-tunnel", "rose-remote"):
        if svc.get(unit) == "active":
            return unit
    return "rose-tunnel" if _systemctl("cat", "rose-tunnel")[0] else "rose-remote"


def remote_start(restart: bool = False) -> dict:
    missing = [f"{r[0]}: {r[2]}" for r in tunnel.readiness() if not r[1]]
    if missing:
        raise RuntimeError("Finish the tunnel setup first: " + "; ".join(missing))
    unit = _remote_unit(health.service_states())
    p = ROSE_HOME / "public_url.txt"
    before = p.read_text().strip() if p.exists() else ""
    p.unlink(missing_ok=True)
    _systemctl("restart" if restart else "start", unit)
    for _ in range(120):
        if p.exists():
            url = p.read_text().strip()
            _probe["t"] = 0
            return {"url": url, "same": url == before}
        time.sleep(1)
    raise RuntimeError("The tunnel didn't come up. Check: rose logs --kind tunnel")


def remote_stop() -> str:
    _systemctl("stop", _remote_unit(health.service_states()))
    _probe["t"] = 0
    return "Remote access stopped."


def set_lockdown(on: bool) -> str:
    guard.set_lockdown(bool(on), "desktop")
    return f"Lockdown {'ON' if on else 'off'}."


def end_grant() -> str:
    return "Grant ended." if guard.end_grant("desktop") else "No grant was active."


def save_tunnel(provider: str, domain: str) -> str:
    dom = domain.strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
    if dom and not tunnel.DOMAIN_RE.match(dom):
        raise ValueError(f"'{dom}' doesn't look like a domain (e.g. abc-def.ngrok-free.app).")
    update_settings({"tunnel_provider": "ngrok" if provider == "ngrok" else "cloudflare", "ngrok_domain": dom})
    return "Tunnel settings saved. Stop and start remote access to switch."


def restart_server() -> str:
    ok, out = _systemctl("restart", "rose")
    if not ok:
        raise RuntimeError(out or "restart failed")
    return "ROSE server restarted."


def approve(id: str, passphrase: str) -> str:
    rec = guard.approve(id, passphrase)
    return "Done." if rec["status"] == "done" else f"Approved, but it failed: {str(rec.get('result', ''))[:200]}"


def deny(id: str) -> str:
    guard.deny(id)
    return "Denied."


def revoke_all() -> str:
    from rose import auth
    auth._cli(["revoke", "all"])
    _probe["t"] = 0
    return "All clients and tokens revoked."


def bridge_post(message: str, to: str = "all") -> str:
    from rose.tools import bridge
    return bridge.post_as("Roshan", message, to)


def bridge_read(last: int = 40) -> list:
    from rose.tools import bridge
    return bridge.bridge_read(last)["messages"]


def face(action: str) -> str:
    # "hide" puts her in the background (still listening); "stop" really stops her.
    args = {"show": [], "hide": ["--hide"], "stop": ["--stop"], "restart": ["--restart"],
            "wake_on": ["--wake", "on"], "wake_off": ["--wake", "off"], "talk": ["--talk"]}[action]
    _detach("rose", "face", *args)
    return "OK"


_MK = "org.gnome.settings-daemon.plugins.media-keys"
_MK_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"


def _gs(*args: str) -> str:
    return subprocess.run(["gsettings", *args], capture_output=True, text=True, timeout=5).stdout.strip()


def _wheel_entry() -> tuple[list[str], str]:
    """(all custom-shortcut paths, the one that runs `rose face --wheel` or "")."""
    paths = re.findall(r"'([^']+)'", _gs("get", _MK, "custom-keybindings"))
    for path in paths:
        if "face --wheel" in _gs("get", f"{_MK}.custom-keybinding:{path}", "command"):
            return paths, path
    return paths, ""


def _wheel_shortcut() -> str:
    """The GNOME custom shortcut that runs `rose face --wheel` (e.g. "<Super>c"), or ""."""
    try:
        _, path = _wheel_entry()
        return _gs("get", f"{_MK}.custom-keybinding:{path}", "binding").strip("'") if path else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def wheel_set_shortcut(binding: str) -> dict:
    """Set (or with "" remove) the desktop shortcut that opens the wheel: a GNOME custom shortcut running
    `rose face --wheel`. Refuses a combination another GNOME shortcut already uses."""
    binding = (binding or "").strip()
    if binding and not re.fullmatch(r"(<(Super|Primary|Control|Alt|Shift)>)*[A-Za-z0-9_]+", binding):
        raise ValueError(f"not a shortcut: {binding!r}")
    paths, path = _wheel_entry()
    key = lambda p: f"{_MK}.custom-keybinding:{p}"  # noqa: E731
    if binding:
        want = binding.lower()
        schemas = ("org.gnome.desktop.wm.keybindings", "org.gnome.shell.keybindings", "org.gnome.mutter.keybindings",
                   "org.gnome.mutter.wayland.keybindings", _MK)
        for line in _gs("list-recursively").splitlines():
            parts = line.split(" ", 2)
            if len(parts) == 3 and parts[0] in schemas and want in [v.lower() for v in re.findall(r"'([^']*)'", parts[2])]:
                raise ValueError(f"{binding} is already used by GNOME ({parts[1]}). Pick another.")
        for p in paths:
            if p != path and _gs("get", key(p), "binding").strip("'").lower() == want:
                raise ValueError(f"{binding} is already the shortcut for {_gs('get', key(p), 'name')}. Pick another.")
    if not binding:
        if path:
            _gs("set", _MK, "custom-keybindings", str([p for p in paths if p != path]))
        return wheel_info()
    if not path:
        n = 0
        while f"{_MK_PATH}custom{n}/" in paths:
            n += 1
        path = f"{_MK_PATH}custom{n}/"
        rose = Path.home() / ".local/bin/rose"
        _gs("set", key(path), "name", "Rose wheel")
        _gs("set", key(path), "command", f"{rose if rose.exists() else 'rose'} face --wheel")
        _gs("set", _MK, "custom-keybindings", str(paths + [path]))
    _gs("set", key(path), "binding", binding)
    return wheel_info()


def wheel_info() -> dict:
    """The menu wheel: running, started with Rose's face, floating button shown, desktop shortcut."""
    from rose import companion

    return {"running": companion._wheel_pid() is not None, "enabled": bool(companion._state().get("wheel", True)),
            "button": companion.wheel_button_shown(), "shortcut": _wheel_shortcut(),
            "available": companion.wheel_launcher() is not None}


def wheel_do(action: str) -> dict:
    """toggle: open/close the circle (starts the wheel if needed); start/stop: run it with Rose's face or not."""
    from rose import companion

    if action == "toggle":
        companion.trigger_wheel()
    elif action == "start":
        companion._save_state(wheel=True)
        companion.start_wheel()
    elif action == "stop":
        companion._save_state(wheel=False)
        companion.stop_wheel()
    else:
        raise ValueError(f"unknown wheel action {action!r}")
    time.sleep(0.6)
    return wheel_info()


def wheel_button(on: bool | None = None) -> dict:
    """The menu wheel's floating button: read (on=None) or show/hide it. Super+C opens the wheel either way."""
    from rose import companion

    if on is not None:
        companion.set_wheel_button(bool(on))
    return {"shown": companion.wheel_button_shown()}


def open_path(what: str) -> str:
    target = {"logs": LOG_DIR, "config": CONFIG_PATH, "data": ROSE_HOME,
              "screenshots": ROSE_HOME / "screenshots", "recordings": ROSE_HOME / "recordings"}[what]
    _detach("xdg-open", str(target))
    return "Opened."


def open_url(url: str) -> str:
    if not url.startswith(("https://", "http://")):
        raise ValueError("http(s) only")
    _detach("xdg-open", url)
    return "Opened."


def logs(kind: str = "tools", n: int = 200) -> str:
    if kind in ("rose", "rose-watch", "rose-tunnel", "rose-phone"):
        r = subprocess.run(["journalctl", "--user", "-u", kind, "-n", str(n), "--no-pager", "-o", "short-iso"],
                           capture_output=True, text=True, timeout=20)
        return r.stdout[-200_000:]
    r = subprocess.run(["rose", "logs", "-n", str(n), "--kind", kind], capture_output=True, text=True, timeout=20)
    return (r.stdout + r.stderr)[-200_000:]


# ---------------- laptop controls ----------------

def laptop_state() -> dict:
    from rose.tools import device, quick
    out = {"quick": quick.quick_settings()}
    try:
        out["media"] = device.media_control("status")
    except Exception as e:  # no player is fine
        out["media"] = str(e)
    try:
        out["audio"] = device.audio_devices()
    except Exception as e:
        out["audio"] = {"error": str(e)}
    return out


def quick_set(setting: str, value: str, confirm: bool = False) -> str:
    from rose.tools import quick
    return quick.set_quick_setting(setting, str(value), confirm=confirm)


def media(action: str) -> object:
    from rose.tools import device
    return device.media_control(action)


def set_audio(device_name: str, kind: str = "output") -> str:
    from rose.tools import device
    return device.set_audio_device(device_name, kind)


def lock_screen() -> str:
    from rose.tools import quick
    return quick.lock_screen()


def power(action: str, confirm: bool = False) -> object:
    from rose.tools import power as p
    return p.power_control(action, confirm=confirm)


def commands() -> dict:
    from rose.tools import device
    return device.list_commands()


def run_command(name: str) -> dict:
    from rose.tools import device
    return device.run_command(name)


def projects(query: str = "") -> list:
    from rose.tools import projects as p
    return p.list_projects(query)


def open_project(path: str) -> str:
    """Open one listed project (exact path, so duplicate names aren't ambiguous) in VS Code."""
    from rose.tools import projects as p
    if not any(x["path"] == path for x in p.list_projects("")):
        raise ValueError("Not a known project.")
    p.open_in_code(Path(path))
    return f"Opened {Path(path).name} in VS Code."


# ---------------- automations / memory / activity ----------------

def routines() -> dict:
    from rose.tools import routines as r
    return r.list_routines()


def run_routine(name: str) -> str:
    from rose.tools import routines as r
    return r.run_routine(name)


def schedules() -> list:
    from rose import watch
    return watch.schedule_list()


def schedule_add(kind: str, time_hhmm: str, text: str = "", repeat: str = "once", date: str = "",
                 weekday: str = "") -> dict:
    from rose import watch
    return watch.schedule_add(kind, time_hhmm, text, repeat=repeat, date=date, weekday=weekday,
                              confirm=kind == "forge")


def schedule_remove(id: str) -> str:
    from rose import watch
    return watch.schedule_remove(id)


def watch_status() -> dict:
    from rose import watch
    return watch.watch_status()


def activity(date: str = "") -> dict:
    from rose import activity as a
    return a.activity_today(date)


def standup(date: str = "") -> str:
    from rose import activity as a
    return a.standup(date)


def undo_history(limit: int = 15) -> list:
    from rose.tools import undo
    return undo.undo_history(limit)


def undo_last() -> str:
    from rose.tools import undo
    return undo.undo_last(1, confirm=True)


def memory_search(query: str = "", limit: int = 30) -> list:
    from rose.tools import memory
    return memory.memory_search(query, limit) if query.strip() else memory.memory_list("", limit)


def memory_save(text: str, tags: str = "") -> dict:
    from rose.tools import memory
    return memory.memory_save(text, tags)


def memory_forget(id: int) -> str:
    from rose.tools import memory
    return memory.memory_forget(int(id), confirm=True)


# ---------------- phone ----------------

def phone_devices() -> dict:
    """Phones over ADB plus how they're linked (usb / wifi / tailscale / offline) and the Tailscale fallback."""
    from rose.tools import phone
    link = phone.link_status()
    devices = phone.phone_devices()
    return {"devices": devices, **link}


def phone_connect(address: str, pairing_code: str = "", pairing_address: str = "") -> str:
    from rose.tools import phone
    return phone.phone_connect(address.strip(), pairing_code.strip(), pairing_address.strip())


def phone_qr_start() -> dict:
    """Show a QR for Wireless debugging → Pair device with QR code; pairs + connects in the background."""
    from rose import phone_qr
    return phone_qr.start()


def phone_qr_status() -> dict:
    from rose import phone_qr
    return phone_qr.status()


def phone_qr_cancel() -> str:
    from rose import phone_qr
    return phone_qr.cancel()


class _PhoneStream:
    """Live phone screen: `adb exec-out screenrecord --output-format=h264 -` decoded by ffmpeg into JPEG frames.
    Much faster than a PNG screencap per frame (~15 fps, ~0.3 s behind). screenrecord stops by itself after
    3 minutes, so it is restarted; the stream stops when nobody asked for a frame for 5 s."""

    def __init__(self):
        self.cond = threading.Condition()
        self.frame = None   # (seq, jpeg bytes)
        self.seq = 0
        self.size = None    # the phone's real (w, h): clicks map to these pixels
        self.thread = None
        self.procs = []
        self.last_ask = 0.0
        self.error = ""
        self.side = 1100
        self.started = 0.0
        self.failed_at = 0.0
        self.frame_at = 0.0
        self.source = "screen"  # or "back" / "front": a phone camera through scrcpy-server
        self.legacy = False     # True: scrcpy-server failed, use screenrecord (slower) for the screen
        self.screen = None      # ScrcpyScreen while the fast mirror runs: input goes through its control socket
        self.gen = 0            # bumped on every restart; an old thread stops when it no longer matches
        self.session = None

    RETRY = 20.0  # after a failed stream, try the fast stream again this often (screenshots meanwhile)

    def ensure(self, side: int, source: str = "screen") -> None:
        self.last_ask = time.time()
        same = side == self.side and source == self.source
        if self.thread and self.thread.is_alive() and same:
            return
        if self.error and same and time.time() - self.failed_at < self.RETRY:
            return  # stay on screenshots (screen) / keep reporting the error (camera) for now
        self.gen += 1
        self.stop()
        if not same:
            self.error, self.frame, self.size = "", None, None
        self.side, self.source, self.started = side, source, time.time()
        self.thread = threading.Thread(target=self._run, args=(self.gen,), daemon=True)
        self.thread.start()

    def fail(self, why: str) -> None:
        self.error, self.failed_at = why, time.time()
        _log_error("phone_stream", RuntimeError(why))

    def stop(self) -> None:
        for p in self.procs:
            try:
                p.kill()
            except OSError:
                pass
        self.procs = []
        if self.session:
            self.session.stop()
            self.session = None
        if self.screen:
            self.screen.stop()
            self.screen = None

    def _real_size(self) -> tuple[int, int]:
        from rose.tools import phone
        import re

        out = str(phone._adb("shell", "wm", "size", timeout=10))
        m = re.findall(r"(\d+)x(\d+)", out)
        w, h = (int(m[-1][0]), int(m[-1][1])) if m else (1080, 2400)
        return w, h

    def _run(self, gen: int) -> None:
        from rose.tools import phone

        if self.source != "screen":
            return self._run_camera(gen)
        if not self.legacy:
            if self._run_display(gen):
                return
            if gen != self.gen:
                return
            self.legacy = True  # scrcpy-server failed: screenrecord + adb input from now on
        try:
            size = self._real_size()
            if gen != self.gen:
                return
            self.size = size
            serial = phone._pick_serial()
            if serial == phone.NO_PHONE:
                raise RuntimeError("No phone connected.")
        except Exception as e:
            self.fail(str(e))
            return
        w, h = self.size
        scale = min(1.0, self.side / max(w, h))
        sw, sh = max(8, int(w * scale) // 8 * 8), max(8, int(h * scale) // 8 * 8)
        adb = ["adb", *(["-s", serial] if serial else []), "exec-out", "screenrecord", "--output-format=h264",
               f"--size={sw}x{sh}", "--bit-rate=6000000", "-"]
        # no "-fflags nobuffer": with it this ffmpeg decodes zero frames from screenrecord's stream
        ff = ["ffmpeg", "-loglevel", "error", "-flags", "low_delay", "-probesize", "32",
              "-analyzeduration", "0", "-f", "h264", "-i", "-", "-pix_fmt", "yuvj420p", "-q:v", "5", "-f", "image2pipe",
              "-c:v", "mjpeg", "-"]
        while time.time() - self.last_ask < 5 and gen == self.gen:
            rec = subprocess.Popen(adb, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            dec = subprocess.Popen(ff, stdin=rec.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            rec.stdout.close()
            self.procs = [rec, dec]
            buf = b""
            got = False
            while time.time() - self.last_ask < 5 and gen == self.gen:
                chunk = dec.stdout.read1(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    a = buf.find(b"\xff\xd8")
                    z = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
                    if a < 0 or z < 0:
                        break
                    jpg, buf = buf[a:z + 2], buf[z + 2:]
                    got = True
                    self.error = ""  # the fast stream works (again)
                    with self.cond:
                        self.frame_at = time.time()
                        self.seq += 1
                        self.frame = (self.seq, jpg)
                        self.cond.notify_all()
            if gen != self.gen:
                return  # replaced by a newer stream; it owns the processes now
            self.stop()
            if not got:
                why = b""
                for p in (rec, dec):
                    try:
                        why += p.stderr.read(400) if p.stderr else b""
                    except (OSError, ValueError):
                        pass
                self.fail("The phone's screen recorder gave no picture: "
                          + (why.decode(errors="replace").strip()[-300:] or "no error text (protected screen?)"))
                return
        self.stop()

    def _publish(self, jpg: bytes) -> None:
        self.error = ""
        with self.cond:
            self.frame_at = time.time()
            self.seq += 1
            self.frame = (self.seq, jpg)
            self.cond.notify_all()

    def _split_jpegs(self, dec, gen: int, on_frame) -> int:
        n, buf = 0, b""
        while gen == self.gen:
            chunk = dec.stdout.read1(65536)
            if not chunk:
                break
            buf += chunk
            while True:
                a = buf.find(b"\xff\xd8")
                z = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
                if a < 0 or z < 0:
                    break
                jpg, buf = buf[a:z + 2], buf[z + 2:]
                n += 1
                on_frame(jpg)
        return n

    def _run_display(self, gen: int) -> bool:
        """Fast mirror through scrcpy-server. Returns False if it couldn't start (caller falls back)."""
        from rose.tools import phone_guard

        scr = phone_guard.ScrcpyScreen(max_size=self.side)
        try:
            scr.start()
        except Exception as e:
            _log_error("phone_stream", RuntimeError(f"scrcpy screen failed, using screenrecord: {e}"))
            return False
        if gen != self.gen:
            scr.stop()
            return True
        self.screen = scr
        ff = ["ffmpeg", "-loglevel", "fatal", "-flags", "low_delay", "-probesize", "32", "-analyzeduration", "0",
              "-f", "h264", "-i", "-", "-pix_fmt", "yuvj420p", "-q:v", "5", "-f", "image2pipe", "-c:v", "mjpeg", "-"]
        dec = subprocess.Popen(ff, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.procs = [dec]

        def pump():
            for data in scr.packets():
                if gen != self.gen or time.time() - self.last_ask > 5:
                    break
                try:
                    dec.stdin.write(data)
                    dec.stdin.flush()
                except (OSError, ValueError):
                    break
            try:
                dec.stdin.close()
            except OSError:
                pass
            if gen == self.gen:
                self.stop()

        threading.Thread(target=pump, daemon=True).start()

        def on_frame(jpg):
            self.size = scr.size  # clicks map to video pixels; the control socket expects exactly these
            self._publish(jpg)

        self._split_jpegs(dec, gen, on_frame)
        return True

    def _run_camera(self, gen: int) -> None:
        """A phone camera (scrcpy-server, raw H.264) decoded by ffmpeg into JPEG frames."""
        from PIL import Image as PILImage
        from rose.tools import phone_guard

        try:
            sess = phone_guard.Scrcpy(camera=self.source, max_size=self.side, bit_rate=3_000_000)
            sess.start()
        except Exception as e:
            self.fail(str(e))
            return
        if gen != self.gen:
            sess.stop()
            return
        self.session = sess
        ff = ["ffmpeg", "-loglevel", "error", "-flags", "low_delay", "-probesize", "32", "-analyzeduration", "0",
              "-f", "h264", "-i", "-", "-pix_fmt", "yuvj420p", "-q:v", "5", "-f", "image2pipe", "-c:v", "mjpeg", "-"]
        dec = subprocess.Popen(ff, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.procs = [dec]

        def pump():
            while gen == self.gen and time.time() - self.last_ask < 5:
                chunk = sess.read()
                if not chunk:
                    break
                try:
                    dec.stdin.write(chunk)
                    dec.stdin.flush()
                except (OSError, ValueError):
                    break
            try:
                dec.stdin.close()
            except OSError:
                pass
            if gen == self.gen:
                self.stop()

        threading.Thread(target=pump, daemon=True).start()
        buf = b""
        while gen == self.gen:
            chunk = dec.stdout.read1(65536)
            if not chunk:
                break
            buf += chunk
            while True:
                a = buf.find(b"\xff\xd8")
                z = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
                if a < 0 or z < 0:
                    break
                jpg, buf = buf[a:z + 2], buf[z + 2:]
                if self.size is None:
                    try:
                        self.size = PILImage.open(BytesIO(jpg)).size
                    except Exception:
                        self.size = (1280, 960)
                self.error = ""
                with self.cond:
                    self.frame_at = time.time()
                    self.seq += 1
                    self.frame = (self.seq, jpg)
                    self.cond.notify_all()

    def get(self, after: int, side: int, wait: float = 1.5, source: str = "screen"):
        self.ensure(side, source)
        end = time.time() + wait
        with self.cond:
            while (self.frame is None or self.frame[0] <= after) and time.time() < end and not self.error:
                self.cond.wait(0.2)
            # no frame since this stream (re)started; a still screen is fine: screenrecord only sends changes
            if not self.error and time.time() - self.started > (15 if self.source != "screen" else 6) \
                    and self.frame_at < self.started:
                self.fail("No live picture from the phone's screen recorder; using screenshots.")
                self.stop()
            return None if self.error else self.frame


_stream = _PhoneStream()


def phone_stream_frame(after: int = 0, max_side: int = 1100, source: str = "screen") -> dict:
    """Newest live frame after `after` (waits up to 1.5 s). source: screen (falls back to a screencap if
    streaming fails) or a camera: back / front."""
    if source not in ("screen", "back", "front"):
        raise ValueError("source must be screen, back or front")
    f = _stream.get(int(after), int(max_side), source=source)
    if f is None:
        if _stream.error and source != "screen":
            raise RuntimeError(_stream.error)
        if _stream.error:
            r = phone_frame(max_side)
            r.update(seq=int(after), fallback=_stream.error)
            return r
        return {"seq": int(after), "same": True}
    if f[0] <= int(after):
        return {"seq": f[0], "same": True}
    w, h = _stream.size
    return {"jpeg": base64.b64encode(f[1]).decode(), "width": w, "height": h, "seq": f[0],
            "control": bool(_stream.screen and _stream.source == "screen")}


def _screen():
    scr = _stream.screen
    if not scr or _stream.source != "screen":
        raise RuntimeError("Fast mirror not running.")
    return scr


def mirror_touch(action: str, x: float, y: float) -> str:
    """Live touch on the mirrored screen (down / move / up), in video pixels."""
    _stream.last_ask = time.time()
    _screen().touch(action, x, y)
    return "ok"


def mirror_key(key: str) -> str:
    _screen().key(key)
    return "ok"


def mirror_text(text: str) -> str:
    _screen().text(text)
    return "ok"


def mirror_scroll(x: float, y: float, dy: float) -> str:
    """Wheel: a quick drag of dy video pixels (positive = content moves up / scroll down)."""
    scr = _screen()
    threading.Thread(target=scr.swipe, args=(x, y, x, y - dy, 160), daemon=True).start()
    return "ok"


def phone_stream_stop() -> str:
    _stream.legacy = False  # next start tries the fast mirror again
    _stream.last_ask = 0
    _stream.stop()
    return "OK"


def phone_frame(max_side: int = 1100, quality: int = 72) -> dict:
    """One mirror frame: JPEG (base64) plus the phone's real size, so clicks map to phone pixels."""
    from PIL import Image
    from rose.tools import phone
    with _frame_lock:  # one screencap at a time; the renderer self-paces
        png = phone._adb("exec-out", "screencap", "-p", binary=True, timeout=20)
    try:
        img = Image.open(BytesIO(png)).convert("RGB")
    except Exception:
        raise RuntimeError("The phone sent no image (screen off, or a protected screen).") from None
    w, h = img.size
    img.thumbnail((max_side, max_side))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=max(30, min(int(quality), 90)))
    return {"jpeg": base64.b64encode(buf.getvalue()).decode(), "width": w, "height": h}


def phone_tap(x: float, y: float) -> str:
    from rose.tools import phone
    return phone.phone_tap(x, y, coords="phone")


def phone_swipe(x1: float, y1: float, x2: float, y2: float, duration_ms: int = 250) -> str:
    from rose.tools import phone
    return phone.phone_swipe(x1, y1, x2, y2, duration_ms, coords="phone")


def phone_longpress(x: float, y: float) -> str:
    from rose.tools import phone
    return phone.phone_swipe(x, y, x, y, 650, coords="phone")


def phone_key(key: str) -> str:
    from rose.tools import phone
    return phone.phone_key(key, confirm=True)  # the user pressed it themselves


def phone_keycode(code: str) -> str:
    from rose.tools import phone
    if not code.replace("_", "").isalnum():
        raise ValueError("bad key")
    phone._shell("input", "keyevent", code)
    return "OK"


def phone_type(text: str) -> str:
    from rose.tools import phone
    return phone.phone_type(text)


def _pc():
    from rose.tools import phone_control
    return phone_control


def phone_status() -> dict:
    return _pc().phone_status()


def phone_volume(action: str = "get", stream: str = "media", level: object = None) -> str:
    kw = {"action": action, "stream": stream}
    if level is not None:
        kw["level"] = level
    return _pc().phone_volume(**kw)


def phone_media(action: str) -> str:
    return _pc().phone_media(action)


def phone_setting(setting: str, value: str = "on", confirm: bool = False) -> str:
    return _pc().phone_setting(setting, str(value), confirm=confirm)


def phone_screen(action: str) -> str:
    return _pc().phone_screen(action)


def phone_notifications(action: str = "list", confirm: bool = False) -> object:
    return _pc().phone_notifications(action, confirm=confirm)


def phone_call_control(action: str) -> str:
    return _pc().phone_call_control(action)


def phone_alarm(time: str = "", timer_minutes: float = 0, label: str = "") -> str:
    return _pc().phone_alarm(time, float(timer_minutes or 0), label)


def phone_open_url(url: str) -> str:
    return _pc().phone_open_url(url)


def phone_open_app(app: str) -> str:
    from rose.tools import phone
    return phone.phone_open_app(app)


def phone_call(who: str) -> str:
    from rose.tools import phone_actions
    return phone_actions.phone_call(who, confirm=True)


def phone_message(who: str, message: str, app: str = "whatsapp") -> str:
    from rose.tools import phone_actions
    return phone_actions.phone_message(who, message, app, confirm=True)


def phone_contacts(query: str) -> list:
    from rose.tools import phone_actions
    return phone_actions.phone_contacts(query)


def phone_send_clipboard() -> str:
    """Type the laptop clipboard into the phone's focused field."""
    from rose.tools import clipboard
    text = clipboard.clipboard_read()
    if isinstance(text, dict):
        text = text.get("text", "")
    return phone_type(str(text)[:1000])


# ---------------- shared theme (same as the phone app) ----------------

def _theme_req(body: dict | None = None) -> dict:
    """GET/POST rose-phone's /api/rose/theme: one theme for the phone app and this window."""
    import urllib.request

    from rose import companion  # tkinter is imported there but no window is opened

    base, token = companion._backend()
    if not token:
        return {}
    req = urllib.request.Request(f"{base}/api/rose/theme", data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def theme_get() -> dict:
    """The shared theme: {theme, accent, tod, theme_updated, hue}; {} when rose-phone is unreachable."""
    try:
        return _theme_req()
    except Exception:
        return {}


def theme_set(theme: str, accent: str = "", tod: dict | None = None) -> dict:
    """Share this window's theme with the phone app."""
    body = {"theme": theme, "accent": str(accent or ""), "client": "desktop"}
    if tod:
        body["tod"] = tod
    try:
        return _theme_req(body)
    except Exception:
        return {}


# ---------------- Rose (the desktop face): look, animation, colours, voice ----------------

def _phone_req(path: str, body: dict | None = None, timeout: float = 10, raw: bool = False):
    import urllib.request

    from rose import companion

    base, token = companion._backend()
    if not token:
        raise RuntimeError("No rose-phone app token (companion_env_file in ~/.config/rose/config.toml).")
    req = urllib.request.Request(f"{base}{path}", data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return data if raw else json.loads(data)


def rose_info() -> dict:
    """Everything the Rose tab shows: her look, the toggles, which face runs, the avatar art for the preview."""
    from rose import companion, face_electron, face_look

    st = companion._state()
    try:
        from rose import wake

        wake_ok = wake.available()
    except Exception:
        wake_ok = False
    assets = Path(companion.__file__).resolve().parent / "assets"
    return {"look": face_look.load(), "defaults": face_look.DEFAULTS, "skins": list(face_look.SKINS),
            "speak": bool(st.get("speak", False)), "caveman": bool(st.get("caveman", True)),
            "wake": bool(st.get("wake", True)) and wake_ok, "wake_available": wake_ok,
            "ui": st.get("face_ui", "electron"), "electron_available": face_electron.launcher() is not None,
            "running": companion._running_pid() is not None, "hotkey": companion.hotkey_spec(),
            "hidden": bool(st.get("hidden")),  # running but closed away: no windows, still listening
            "avatar": (assets / "rose_avatar.svg").read_text(), "body": (assets / "rose_body.svg").read_text(),
            "views": list(face_look.VIEWS)}


def rose_look_set(changes: dict) -> dict:
    from rose import face_look

    return face_look.save(changes)


def rose_look_reset() -> dict:
    from rose import face_look

    return face_look.reset()


def rose_setting(key: str, value: object) -> str:
    """speak / caveman: saved (a running face follows at once). wake / ui: saved, and the face restarts."""
    from rose import companion

    if key in ("speak", "caveman"):
        companion._save_state(**{key: bool(value)})
        return "OK"
    if key == "wake":
        face("wake_on" if value else "wake_off")
        return "OK"
    if key == "ui":
        if value not in ("electron", "tk"):
            raise ValueError("ui is electron or tk")
        _detach("rose", "face", "--ui", str(value))
        return "OK"
    raise ValueError(f"unknown setting {key!r}")


def rose_voices() -> list:
    """Her voices (rose-phone /api/config): Groq's natural ones first, then the laptop's Piper voices."""
    return _phone_req("/api/config").get("tts_voices", [])


def rose_test_voice(voice: str = "", rate: float = 1.0, text: str = "") -> str:
    """Say a line with this voice on the laptop speakers."""
    import tempfile

    body = {"text": (text or "Hii! I'm Rose. This is how I sound.")[:300], "rate": max(0.5, min(2.0, float(rate)))}
    if voice:
        body["voice"] = voice
    audio = _phone_req("/api/tts", body, timeout=60, raw=True)
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        f.write(audio)
        f.flush()
        subprocess.run(["paplay", f.name], timeout=60)
    return "OK"


# ---------------- phone: find my phone (rose/tools/phone_guard.py) ----------------

def _pg():
    from rose.tools import phone_guard
    return phone_guard


def guard_location(refresh: bool = False) -> dict:
    return _pg().phone_location(refresh=bool(refresh))


def guard_photo(camera: str = "front") -> dict:
    g = _pg()
    jpg = g._grab_jpeg(camera)
    path = g._out(f"photo-{g._stamp()}-{camera}.jpg")
    path.write_bytes(jpg)
    return {"jpeg": base64.b64encode(jpg).decode(), "path": str(path)}


def guard_record(seconds: int = 15) -> str:
    return _pg().phone_mic_record(int(seconds))


_listen = {"obj": None}


def guard_listen(on: bool = True) -> dict:
    """Phone microphone live on the laptop speakers."""
    cur = _listen["obj"]
    if not on:
        if cur:
            cur.stop()
        _listen["obj"] = None
        return {"on": False}
    if cur and cur.running:
        return {"on": True, "level": cur.level}
    obj = _pg().MicListen()
    obj.start()
    _listen["obj"] = obj
    return {"on": True, "level": obj.level}


def guard_listen_level() -> dict:
    cur = _listen["obj"]
    return {"on": bool(cur and cur.running), "level": cur.level if cur else -120.0}


_talk = {"proc": None, "path": None}


def guard_talk(on: bool = True, volume: int = 100) -> str:
    """Push-to-talk: record the laptop microphone while on, then play it on the phone."""
    g = _pg()
    if on:
        if _talk["proc"]:
            return "Already recording."
        path = g._out(f"talk-{g._stamp()}.wav")
        _talk["path"] = path
        _talk["proc"] = subprocess.Popen(["pw-record", "--rate", "48000", "--channels", "1", str(path)],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "Recording… release to send."
    proc, path = _talk["proc"], _talk["path"]
    _talk["proc"] = None
    if not proc:
        return "Not recording."
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    if not path or not path.exists() or path.stat().st_size < 9600:
        raise RuntimeError("Recording too short.")
    return g.play_on_phone(g._to_mp3(path), int(volume))


def guard_say(text: str = "", siren: bool = False, volume: int = 100) -> str:
    return _pg().phone_speak(text=text, siren=bool(siren), volume=int(volume), confirm=True)


def guard_stop_sound() -> str:
    return _pg().phone_stop_sound()


def guard_power(action: str) -> str:
    return _pg().phone_power(action, confirm=True)


def guard_lost_mode(message: str = "") -> dict:
    res = _pg().phone_lost_mode(message=message, siren=True, confirm=True)
    out = {"text": res[0]}
    if len(res) > 1:
        out["jpeg"] = base64.b64encode(res[1].data).decode()
    return out


def guard_find_hub() -> str:
    return open_url(_pg().FIND_HUB)


def today(refresh: bool = False) -> dict:
    """Today's calendar events and inbox (rose.agenda; set up with `rose agenda setup`)."""
    from rose import agenda
    return agenda.today(refresh=bool(refresh))


def failure_summary(days: int = 7) -> dict:
    """Failed tool calls and "I can't" replies (rose.failures)."""
    from rose import failures
    r = failures.report(int(days))
    r.pop("markdown", None)
    return r


METHODS = {name: fn for name, fn in globals().items()
           if callable(fn) and not name.startswith("_") and getattr(fn, "__module__", "") == __name__}


# ---------------- loop ----------------

def _send(obj: dict) -> None:
    line = json.dumps(obj, default=str)
    with _out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _handle(req: dict) -> None:
    rid = req.get("id")
    fn = METHODS.get(req.get("method", ""))
    if fn is None:
        _send({"id": rid, "error": f"unknown method {req.get('method')!r}"})
        return
    try:
        _send({"id": rid, "result": fn(**(req.get("params") or {}))})
    except Exception as e:  # every error goes back to the window as text
        _send({"id": rid, "error": str(e) or type(e).__name__})
        _log_error(req.get("method", ""), e)


BACKEND_LOG = Path.home() / "rose" / "desktop-backend.log"


def _log_error(method: str, e: Exception) -> None:
    """Keep the traceback: the window only gets the message, and Electron's stderr isn't saved anywhere."""
    import traceback
    try:
        if BACKEND_LOG.exists() and BACKEND_LOG.stat().st_size > 1_000_000:
            BACKEND_LOG.replace(BACKEND_LOG.with_suffix(".log.1"))
        with BACKEND_LOG.open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {method}: {type(e).__name__}: {e}\n"
                    + "".join(traceback.format_exception(e)) + "\n")
    except OSError:
        pass


def main() -> int:
    pool = ThreadPoolExecutor(max_workers=8)
    _send({"ready": True, "methods": sorted(METHODS)})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if str(req.get("method", "")).startswith("mirror_"):
            _handle(req)  # live touches: in arrival order, no thread-pool reordering (each is one socket write)
        else:
            pool.submit(_handle, req)
    return 0


if __name__ == "__main__":
    sys.exit(main())
