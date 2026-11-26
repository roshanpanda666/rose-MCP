"""Rose on the desktop: a small round, always-on-top dial with her animated face (`rose face`).

* 140 px round window (X Shape extension) with a glowing ring that spins in her mood's colours.
* Drawn with PIL at 3x and scaled down every frame: smooth edges, gradients, glow (Tk's own canvas
  shapes are jagged at this size).
* Her eyes follow the mouse pointer on every monitor; she blinks, winks and glances around.
* Her expression follows the screen, re-checked every 2 s: thinking in VS Code/terminals, enjoying music
  (MPRIS), watching videos, chatty in chat apps, reading, worried at errors, sleepy late/idle.
* Click the dial: a small rounded chat box opens beside it. It is a normal (managed, undecorated) window so it
  gets keyboard focus: the old frameless window could never be typed into. It talks to the same Rose as the
  phone app (rose-phone /api/chat), with her tools and approvals.
* Drag the dial to move it. Right-click: speak replies, caveman mode, new conversation, send a file to the
  phone (clipboard file / latest download, via rose-phone /api/files/push), hide.
* Global hotkey (config face_hotkey, default ctrl+alt+r; Super+R via a GNOME shortcut; also `rose face --talk`): show her, open the chat and
  push-to-talk (Whisper via rose-phone); press again to cancel.
* She shows Claude Code's state (~/rose/claude_state.json: working / needs you / finished) and, now and then,
  worries while CI is failing (~/rose/watch_state.json).

Tk rule (see gui.py): never touch widgets from a worker thread. Workers put results on a queue; the
animation loop on the main thread applies them.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import queue
import random
import re
import signal
import subprocess
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from rose.config import ROSE_HOME, load_config

D = 140  # fallback round dial (no librsvg) size on screen (px)
C = 420  # drawing size (the avatar SVG's own units); frames are scaled down to the window
CROP = (60, 26, 360, 419)  # her bust inside the 420 canvas (shown without any dial/ring/border)
OW, OH = 100, 131  # the bust window on screen
MARGIN = 40
FPS = 24
STATE_FILE = ROSE_HOME / "companion.json"
POP_W, POP_H = 300, 196

BG = "#15111b"
CARD = "#201a29"
FIELD = "#2b2335"
TEXT = "#f6eef3"
MUTED = "#a897ad"
ACCENT = "#ff4f8b"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# palette (RGB)
SKIN = (255, 222, 206)
SKIN_SH = (240, 188, 170)
HAIR_D = (34, 14, 26)
HAIR_M = (70, 24, 48)
HAIR_HI = (138, 56, 92)
LIP = (214, 52, 96)
LIP_D = (150, 26, 62)
IRIS_O = (120, 18, 52)
IRIS_I = (222, 72, 120)
LINER = (28, 10, 20)
TOP = (24, 18, 30)

RING = {  # mood -> ring gradient colours
    "idle": ("#ff4f8b", "#8b5cff"), "thinking": ("#8b5cff", "#38c6ff"), "vibing": ("#ff4f8b", "#ffb347"),
    "focus_music": ("#8b5cff", "#ff4f8b"), "watching": ("#38c6ff", "#ff4f8b"), "chatty": ("#ff4f8b", "#ffd36e"),
    "reading": ("#4fd1a5", "#38c6ff"), "worried": ("#ffb020", "#ff5a5a"), "sleepy": ("#5b6cff", "#272b66"),
    "listening": ("#38c6ff", "#4fd1a5"), "talking": ("#ff4f8b", "#38c6ff"), "working": ("#8b5cff", "#ffb347"),
}
SPIN = {"thinking": 140, "working": 220, "vibing": 90, "focus_music": 70, "sleepy": 8, "talking": 60}

# expression presets: brows (inner, outer raise in px@3x), eye shape, openness, mouth, extra, gaze, bob, label
MOODS = {
    "idle": dict(brow=(0, 0), eye="normal", open=1.0, mouth="smile", extra=None, gaze=None, bob=0, label="hanging out"),
    "thinking": dict(brow=(-6, 6), eye="normal", open=0.8, mouth="hmm", extra="dots", gaze=(0.6, -0.8), bob=0,
                     label="thinking with you"),
    "vibing": dict(brow=(-4, -4), eye="happy", open=1.0, mouth="grin", extra="notes", gaze=None, bob=9,
                   label="enjoying the music"),
    "focus_music": dict(brow=(-5, 4), eye="normal", open=0.85, mouth="smile", extra="notes", gaze=(0.4, -0.5), bob=4,
                        label="coding to music"),
    "watching": dict(brow=(-6, -6), eye="wide", open=1.0, mouth="o", extra=None, gaze=None, bob=0, label="watching too"),
    "chatty": dict(brow=(-4, -4), eye="normal", open=1.0, mouth="grin", extra="sparkle", gaze=None, bob=0,
                   label="chatting"),
    "reading": dict(brow=(-2, 2), eye="normal", open=0.75, mouth="small", extra=None, gaze="scan", bob=0,
                    label="reading along"),
    "worried": dict(brow=(10, -6), eye="normal", open=1.0, mouth="wavy", extra="sweat", gaze=None, bob=0,
                    label="uh-oh, an error?"),
    "sleepy": dict(brow=(2, 2), eye="normal", open=0.22, mouth="small", extra="z", gaze=(0, 0.4), bob=2,
                   label="sleepy"),
    "listening": dict(brow=(-6, -6), eye="normal", open=1.0, mouth="small", extra=None, gaze=(-0.6, 0.6), bob=0,
                      label="listening"),
    "talking": dict(brow=(-4, -4), eye="normal", open=1.0, mouth="talk", extra=None, gaze=None, bob=0,
                    label="talking"),
    "working": dict(brow=(-6, 4), eye="normal", open=0.85, mouth="hmm", extra="dots", gaze=(0.7, -0.4), bob=0,
                    label="working on it"),
    "surprised": dict(brow=(-9, -9), eye="wide", open=1.0, mouth="o", extra=None, gaze=(0, 0.3), bob=0,
                      label="needs your OK!"),
    "proud": dict(brow=(-4, -4), eye="happy", open=1.0, mouth="grin", extra="sparkle", gaze=None, bob=3, label="done!"),
    "sad": dict(brow=(9, -4), eye="normal", open=0.7, mouth="wavy", extra=None, gaze=(0, 0.6), bob=0,
                label="that failed"),
    "love": dict(brow=(-4, -4), eye="happy", open=1.0, mouth="smile", extra="sparkle", gaze=None, bob=3,
                 label="aww, thanks"),
    "spy": dict(brow=(7, -3), eye="normal", open=0.55, mouth="hmm", extra=None, gaze="scan", bob=0, label="listening in"),
    "screen": dict(brow=(-3, -3), eye="normal", open=0.85, mouth="small", extra=None, gaze=(0.1, 1), bob=0,
                   label="looking at the screen"),
    "lookout": dict(brow=(-6, -6), eye="normal", open=1.0, mouth="o", extra=None, gaze=None, bob=0, label="on lookout"),
    "camera": dict(brow=(-3, 3), eye="normal", open=0.9, mouth="smile", extra=None, gaze=(0.3, 0), bob=0,
                   label="say cheese"),
}
BLUSHY = {"vibing", "chatty", "talking", "idle", "focus_music", "listening"}

CODE = re.compile(r"code|vscod|cursor|jetbrains|pycharm|idea|sublime|vim|emacs|zed|terminal|ghostty|kitty|alacritty|"
                  r"konsole|xterm|tilix|wezterm", re.I)
VIDEO = re.compile(r"youtube(?! music)|netflix|prime video|hotstar|twitch|\bvlc\b|mpv|video|movie|episode", re.I)
CHAT = re.compile(r"whatsapp|telegram|discord|slack|signal|messenger|teams|gmail|inbox|outlook|mail", re.I)
READ = re.compile(r"\.pdf|evince|okular|notion|obsidian|docs|document|reader|wikipedia|medium|arxiv", re.I)
ERROR = re.compile(r"\berror\b|failed|exception|traceback|crash", re.I)


# ---------------------------------------------------------------- screen context (worker thread)

def _out(cmd: list[str], timeout: float = 2) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def screen_context() -> dict:
    """Active window title/class, music playing (MPRIS), idle seconds and hour: what Rose reacts to."""
    wid = _out(["xdotool", "getactivewindow"])
    title = _out(["xdotool", "getwindowname", wid]) if wid else ""
    wclass = _out(["xprop", "-id", wid, "WM_CLASS"]) if wid else ""
    music = None
    try:
        from rose.tools import device

        for p in device._players():
            info = device._player_info(p)
            if info.get("status") == "Playing":
                music = " - ".join(x for x in (info.get("artist"), info.get("title")) if x) or info["player"]
                break
    except Exception:
        pass
    idle = 0.0
    xp = _out(["xprintidle"])
    if xp.isdigit():
        idle = int(xp) / 1000
    return {"title": title, "class": wclass, "music": music, "idle": idle, "hour": time.localtime().tm_hour}


def mood_for(ctx: dict, pointer_idle: float) -> tuple[str, str]:
    """(mood, status line) from the screen context."""
    title, both = ctx["title"], f"{ctx['title']} {ctx['class']}"
    idle = max(ctx.get("idle", 0), pointer_idle)
    late = 1 <= ctx["hour"] < 5
    if idle > 300 or (late and idle > 60):
        return "sleepy", "sleepy… (no one around)" if idle > 300 else "it's late, go to sleep"
    if ERROR.search(title):
        return "worried", "uh-oh, an error?"
    coding = bool(CODE.search(both))
    if ctx["music"]:
        if coding:
            return "focus_music", f"coding to {ctx['music'][:40]}"
        if VIDEO.search(both):
            return "watching", "watching too"
        return "vibing", f"♪ {ctx['music'][:44]}"
    if coding:
        return "thinking", "thinking with you"
    if VIDEO.search(both):
        return "watching", "watching too"
    if CHAT.search(both):
        return "chatty", "chatting"
    if READ.search(both):
        return "reading", "reading along"
    return "idle", "hanging out"


# ---------------------------------------------------------------- talking to Rose (worker thread)

def _env_token(path: str) -> str:
    try:
        for line in Path(path).expanduser().read_text().splitlines():
            if line.startswith("APP_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(**kw) -> None:
    s = {**_state(), **kw}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(s))


def _backend() -> tuple[str, str]:
    cfg = load_config()
    base = str(cfg.get("companion_url", "http://127.0.0.1:8790")).rstrip("/")
    return base, _env_token(str(cfg.get("companion_env_file", "~/0x/projects/rose-phone-app/backend/.env")))


def chat(text: str, events: queue.Queue) -> None:
    """Send one message to Rose (rose-phone backend) and stream events back to the UI."""
    base, token = _backend()
    if not token:
        events.put(("error", "No app token: set companion_env_file in ~/.config/rose/config.toml."))
        events.put(("done", ""))
        return
    st = _state()
    # One conversation everywhere: continue the shared thread (the phone follows), unless "New conversation".
    fresh = st.get("fresh", False)
    body = {"message": text, "conversation_id": None, "continue_current": not fresh, "client": "desktop",
            "caveman": st.get("caveman", True)}
    if fresh:
        _save_state(fresh=False)
    req = urllib.request.Request(f"{base}/api/chat", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            for raw in resp:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:])
                except ValueError:
                    continue
                kind = ev.get("type")
                if kind == "start" and ev.get("conversation_id"):
                    _save_state(conversation_id=ev["conversation_id"])
                elif kind == "text":
                    events.put(("delta", ev.get("delta", "")))
                elif kind == "tool_call":
                    events.put(("tool", ev.get("name", "")))
                elif kind == "tool_result":
                    if ev.get("approval_url"):
                        events.put(("approval", ev["approval_url"]))
                    events.put(("tool_done", bool(ev.get("ok"))))
                elif kind == "error":
                    events.put(("error", ev.get("message", "Something went wrong.")))
    except Exception as e:
        events.put(("error", f"Can't reach Rose ({type(e).__name__}). Is rose-phone running?"))
    events.put(("done", ""))


def theme_hue() -> float | None:
    """The phone app's theme hue (it posts it to rose-phone when you change theme)."""
    base, token = _backend()
    if not token:
        return None
    try:
        req = urllib.request.Request(f"{base}/api/rose/theme", headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            hue = json.loads(resp.read()).get("hue")
        return float(hue) if hue is not None else None
    except Exception:
        return None


def _envelope(wav_bytes: bytes, step: float = 0.04) -> list[float]:
    """Loudness (0..1) every `step` seconds of a 16-bit WAV: drives her mouth (lip sync)."""
    import array
    import io
    import wave

    try:
        with wave.open(io.BytesIO(wav_bytes)) as w:
            rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            frames = w.readframes(w.getnframes())
        if width != 2:
            return []
        data = array.array("h", frames)[::ch]
        n = max(1, int(rate * step))
        levels = [math.sqrt(sum(v * v for v in data[i:i + n][::4]) / max(1, len(data[i:i + n][::4]))) / 32768
                  for i in range(0, len(data), n)]
        peak = max(levels, default=0) or 1
        return [min(1.0, v / peak * 1.3) for v in levels]
    except Exception:
        return []


_voice = {"proc": None, "paused": False}  # the paplay saying her reply right now
_voice_lock = threading.Lock()


def speaking() -> bool:
    """Her voice is playing (or paused mid-reply) on the laptop speakers."""
    p = _voice["proc"]
    return p is not None and p.poll() is None


def pause_speech() -> None:
    """Hold her voice where it is (SIGSTOP; paplay runs with a short buffer, so she stops within ~0.1 s)."""
    with _voice_lock:
        p = _voice["proc"]
        if p is not None and p.poll() is None and not _voice["paused"]:
            p.send_signal(signal.SIGSTOP)
            _voice["paused"] = True


def resume_speech() -> None:
    with _voice_lock:
        p = _voice["proc"]
        if p is not None and p.poll() is None and _voice["paused"]:
            p.send_signal(signal.SIGCONT)
        _voice["paused"] = False


def stop_speech() -> None:
    """Cut her off for good."""
    with _voice_lock:
        p = _voice["proc"]
        if p is not None and p.poll() is None:
            p.kill()
            if _voice["paused"]:
                p.send_signal(signal.SIGCONT)
        _voice["paused"] = False


def barge_wanted() -> bool:
    """Talking over her pauses her and she listens (right-click menu; on unless turned off)."""
    return bool(_state().get("barge", True))


def speak(text: str, events: queue.Queue | None = None) -> None:
    """Say a reply on the laptop speakers with Rose's voice (rose-phone /api/tts); her mouth follows it."""
    base, token = _backend()
    if not token or not text.strip():
        return
    req = urllib.request.Request(f"{base}/api/tts", data=json.dumps({"text": text[:1800]}).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, tempfile.NamedTemporaryFile(suffix=".wav") as f:
            audio = resp.read()
            f.write(audio)
            f.flush()
            if events is not None:
                events.put(("lips", _envelope(audio)))
            stop_speech()  # one voice at a time
            with _voice_lock:
                proc = _voice["proc"] = subprocess.Popen(["paplay", "--latency-msec=100", f.name])
                _voice["paused"] = False
            try:
                proc.wait(timeout=300)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        pass


def transcribe(wav: bytes) -> str | None:
    """Speech to text with the phone backend's Whisper (rose-phone /api/transcribe). None if it failed."""
    base, token = _backend()
    if not token:
        return None
    try:
        req = urllib.request.Request(f"{base}/api/transcribe", data=wav, method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "audio/wav"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return str(json.loads(resp.read()).get("text", "") or "")
    except Exception:
        return None


def _log(msg: str) -> None:
    try:
        with open(ROSE_HOME / "companion.log", "a") as f:
            f.write(f"{time.strftime('%F %T')} {msg}\n")
    except OSError:
        pass


def _notify(title: str, body: str) -> None:
    try:
        subprocess.run(["notify-send", "--app-name=ROSE", title, body[:300]], capture_output=True, timeout=5)
    except Exception:
        pass


# ---------------------------------------------------------------- global hotkey (push-to-talk)

def hotkey_spec() -> str:
    """Config `face_hotkey` (default "ctrl+alt+r"; "" or "off" turns it off)."""
    spec = str(load_config().get("face_hotkey", "ctrl+alt+r") or "").strip().lower()
    return "" if spec in ("off", "none", "false", "0") else spec


def gnome_keeps_super() -> bool:
    """GNOME's window manager (mutter) holds the Super key for the Activities overview (overlay-key): it then
    swallows Super+<key> combinations, so an X key grab on them never fires."""
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if "gnome" not in desktop and "unity" not in desktop:
        return False
    out = _out(["gsettings", "get", "org.gnome.mutter", "overlay-key"])
    return out.strip("'\" ") != ""


def trigger_talk() -> int:
    """`rose face --talk`: what the hotkey does (show Rose, open the chat, push-to-talk; again = cancel).
    Bind it to a desktop shortcut when the desktop keeps the hotkey for itself."""
    import signal

    pid = _running_pid()
    if pid is not None:
        os.kill(pid, signal.SIGUSR1)
        return 0
    _save_state(talk_on_start=True)
    start_detached()
    return 0


def trigger_visibility(show: bool) -> int:
    """Show a running Rose or close her to the background (she keeps listening): the choice goes into
    companion.json and SIGUSR2 tells her to apply it. 0 if one was running."""
    import signal

    pid = _running_pid()
    if pid is None:
        return 1
    _save_state(hidden=not show)
    try:
        os.kill(pid, signal.SIGUSR2)
    except OSError:
        return 1
    return 0


def trigger_show() -> int:
    """Bring a running Rose back from the background. 0 if one was running."""
    return trigger_visibility(True)


def trigger_hide() -> int:
    """Put a running Rose in the background: no windows, still listening. 0 if one was running."""
    return trigger_visibility(False)


# ---------------------------------------------------------------- files to the phone (worker thread)

def parse_uri_list(text: str) -> list[str]:
    """Local file paths from a text/uri-list (or GNOME's x-special/gnome-copied-files) clipboard payload."""
    from urllib.parse import unquote, urlparse

    paths = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line in ("copy", "cut"):
            continue
        if line.startswith("file://"):
            u = urlparse(line)
            if u.netloc not in ("", "localhost"):
                continue
            p = unquote(u.path)
        elif line.startswith("/"):
            p = line
        else:
            continue
        if Path(p).is_file():
            paths.append(p)
    return list(dict.fromkeys(paths))


def clipboard_files() -> list[str]:
    """Files copied in the file manager (Ctrl+C on a file), or a plain absolute path copied as text."""
    for target in ("text/uri-list", "x-special/gnome-copied-files", "UTF8_STRING"):
        got = parse_uri_list(_out(["xclip", "-selection", "clipboard", "-t", target, "-o"]))
        if got:
            return got
    return []


PARTIAL = (".crdownload", ".part", ".partial", ".download", ".tmp")


def latest_download(folder: Path | None = None) -> str | None:
    """Newest finished file in ~/Downloads (skips hidden files and downloads still in progress)."""
    folder = folder or Path.home() / "Downloads"
    best = None
    try:
        for p in folder.iterdir():
            if p.name.startswith(".") or p.name.lower().endswith(PARTIAL) or not p.is_file():
                continue
            m = p.stat().st_mtime
            if best is None or m > best[0]:
                best = (m, p)
    except OSError:
        return None
    return str(best[1]) if best else None


def push_file(path: str) -> tuple[bool, str]:
    """Ask the phone backend to send one laptop file to the phone (rose-phone POST /api/files/push)."""
    base, token = _backend()
    if not token:
        return False, "No app token: set companion_env_file in ~/.config/rose/config.toml."
    name = Path(path).name
    req = urllib.request.Request(f"{base}/api/files/push", data=json.dumps({"path": path}).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
        return True, f"Sent {name} to your phone 📱"
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail") or e.reason
        except Exception:
            detail = e.reason
        return False, f"Couldn't send {name}: {str(detail)[:160]}"
    except Exception as e:
        return False, f"Couldn't send {name} ({type(e).__name__}). Is rose-phone running?"


def send_files(source: str, events: queue.Queue) -> None:
    """Menu action (worker thread): source "clipboard" or "download"."""
    if source == "clipboard":
        paths = clipboard_files()[:10]
        if not paths:
            events.put(("notice", ("No file on the clipboard. Copy a file in the file manager first.", False)))
            return
    else:
        p = latest_download()
        if not p:
            events.put(("notice", ("Nothing in Downloads to send.", False)))
            return
        paths = [p]
    msgs, ok_all = [], True
    for p in paths:
        ok, msg = push_file(p)
        ok_all &= ok
        msgs.append(msg)
    events.put(("notice", ("\n".join(msgs), ok_all)))


# ---------------------------------------------------------------- system state (Claude Code, CI)

CLAUDE_STATE = ROSE_HOME / "claude_state.json"
WATCH_STATE = ROSE_HOME / "watch_state.json"
DONE_FRESH = 120  # s: "done" counts as news this long
STALE = 30 * 60  # s: anything older is idle


def _ts(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v) / (1000 if v > 1e12 else 1)
    if isinstance(v, str) and v:
        from datetime import datetime

        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            try:
                return float(v)
            except ValueError:
                return None
    return None


def system_state(claude: dict | None, watch: dict | None, now: float) -> dict:
    """{"claude": "busy"|"waiting"|"done"|"idle", "project", "message", "key", "ci_failing": bool}.
    "done" older than 2 min and anything older than 30 min count as idle."""
    out = {"claude": "idle", "project": "", "message": "", "key": "", "ci_failing": False}
    c = claude if isinstance(claude, dict) else {}
    state = str(c.get("state") or "")
    ts = _ts(c.get("ts"))
    if state in ("busy", "waiting", "done") and ts is not None:
        age = now - ts
        if -60 <= age <= STALE and not (state == "done" and age > DONE_FRESH):
            out.update(claude=state, project=str(c.get("project") or "")[:60], message=str(c.get("message") or "")[:200],
                       key=f"{state}:{c.get('session_id', '')}:{c.get('ts')}")
    w = watch if isinstance(watch, dict) else {}
    out["ci_failing"] = bool(isinstance(w.get("ci_failing"), list) and w["ci_failing"])
    return out


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def read_system_state() -> dict:
    return system_state(_read_json(CLAUDE_STATE), _read_json(WATCH_STATE), time.time())


def bubble_for(prev: dict | None, cur: dict) -> tuple[str, str] | None:
    """(mood, text) to pop up when Claude's state changes, else None."""
    if not cur["key"] or (prev and prev.get("key") == cur["key"]):
        return None
    where = f" in {cur['project']}" if cur["project"] else ""
    if cur["claude"] == "waiting":
        return "surprised", f"Claude needs you{where}"
    if cur["claude"] == "done":
        return "proud", f"Claude finished{where}"
    return None


# ---------------------------------------------------------------- drawing (PIL, main thread)

def _hex(c: str) -> tuple[int, int, int]:
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def _mix(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _smooth(pts: list[tuple[float, float]], steps: int = 10, closed: bool = True) -> list[tuple[float, float]]:
    """Catmull-Rom spline through the points (PIL draws straight segments only)."""
    out = []
    n = len(pts)
    rng = range(n) if closed else range(n - 1)
    for i in rng:
        p0, p1, p2, p3 = (pts[(i - 1) % n], pts[i], pts[(i + 1) % n], pts[(i + 2) % n]) if closed else \
            (pts[max(i - 1, 0)], pts[i], pts[i + 1], pts[min(i + 2, n - 1)])
        for s in range(steps):
            t = s / steps
            t2, t3 = t * t, t * t * t
            out.append(tuple(0.5 * (2 * p1[k] + (-p0[k] + p2[k]) * t + (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * t2
                                    + (-p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]) * t3) for k in (0, 1)))
    if not closed:
        out.append(pts[-1])
    return out


class Painter:
    """Builds the static layers once; draws one frame per call."""

    def __init__(self):
        from PIL import Image, ImageDraw, ImageFilter, ImageFont

        self.Image, self.ImageDraw, self.ImageFilter = Image, ImageDraw, ImageFilter
        try:
            self.font = ImageFont.truetype(FONT_BOLD, 34)
            self.small = ImageFont.truetype(FONT_BOLD, 26)
        except OSError:
            self.font = self.small = ImageFont.load_default()
        self.cx, self.cy = C / 2, 196  # face centre
        self.inner = self._circle_mask(C / 2 - 16)
        self.bg = self._background()
        self.char = self._character()
        self.blush = self._blush()
        self.rings: dict[str, object] = {}

    def _circle_mask(self, r: float):
        m = self.Image.new("L", (C, C), 0)
        self.ImageDraw.Draw(m).ellipse((C / 2 - r, C / 2 - r, C / 2 + r, C / 2 + r), fill=255)
        return m

    def _background(self):
        Image, ImageDraw = self.Image, self.ImageDraw
        bg = Image.new("RGBA", (C, C), (0, 0, 0, 0))
        d = ImageDraw.Draw(bg)
        for i in range(60):  # radial gradient: deep plum to near-black
            t = i / 59
            r = (C / 2 - 4) * (1 - t)
            col = _mix((48, 22, 52), (14, 10, 18), 1 - t)
            d.ellipse((C / 2 - r, C / 2 - r, C / 2 + r, C / 2 + r), fill=col + (255,))
        return bg

    def _character(self):
        Image, ImageDraw = self.Image, self.ImageDraw
        cx, cy = self.cx, self.cy
        im = Image.new("RGBA", (C, C + 40), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        # long back hair (gradient strands)
        for i, (x0, x1) in enumerate(((92, 172), (248, 328))):
            d.rounded_rectangle((x0, 150, x1, 470), radius=40, fill=HAIR_D + (255,))
        d.ellipse((92, 58, 328, 320), fill=HAIR_D + (255,))
        # shoulders: sleek black top, rose trim, V neckline
        d.ellipse((36, 318, 384, 540), fill=TOP + (255,))
        d.arc((36, 318, 384, 540), 200, 340, fill=(214, 52, 96, 255), width=5)
        d.rounded_rectangle((184, 262, 236, 340), radius=18, fill=SKIN_SH + (255,))
        d.polygon([(170, 326), (250, 326), (210, 384)], fill=SKIN_SH + (255,))
        d.arc((176, 300, 244, 356), 30, 150, fill=(255, 214, 140, 255), width=3)  # necklace
        d.line([(210, 356), (210, 372)], fill=(255, 210, 120, 255), width=3)
        # face with a soft jaw
        pts = []
        for k in range(120):
            th = 2 * math.pi * k / 120
            taper = 1 - 0.24 * max(0.0, math.sin(th)) ** 2.2
            pts.append((cx + 74 * math.cos(th) * taper, cy + 96 * math.sin(th)))
        d.polygon(pts, fill=SKIN + (255,))
        d.ellipse((cx - 30, cy + 70, cx + 30, cy + 98), fill=SKIN + (255,))
        # cheek contour
        d.arc((cx - 70, cy - 10, cx - 30, cy + 80), 120, 200, fill=SKIN_SH + (255,), width=3)
        d.arc((cx + 30, cy - 10, cx + 70, cy + 80), 340, 60, fill=SKIN_SH + (255,), width=3)
        # nose
        d.line(_smooth([(cx + 4, cy + 20), (cx - 2, cy + 40), (cx + 6, cy + 46)], 6, closed=False),
               fill=SKIN_SH + (255,), width=4)
        # side-swept bangs (glossy)
        bang = _smooth([(124, 236), (122, 150), (152, 96), (210, 76), (274, 90), (306, 138), (308, 230),
                        (292, 176), (272, 146), (246, 132), (220, 152), (196, 136), (172, 158), (148, 186)], 8)
        d.polygon(bang, fill=HAIR_M + (255,))
        d.line(_smooth([(150, 128), (196, 98), (248, 96), (288, 122)], 8, closed=False), fill=HAIR_HI + (255,),
               width=7)
        d.line(_smooth([(170, 112), (214, 92), (262, 100)], 8, closed=False), fill=(190, 96, 136, 200), width=3)
        # gold hoop earring
        d.ellipse((128, 262, 150, 284), outline=(255, 205, 110, 255), width=4)
        # the rose in her hair
        rx, ry = 286, 112
        for i, (ox, oy, r) in enumerate(((0, 0, 20), (-11, -6, 13), (11, -5, 13), (0, 9, 13), (-3, -2, 8))):
            col = ((190, 20, 70), (220, 40, 96), (236, 60, 116), (160, 16, 60), (120, 8, 46))[i]
            d.ellipse((rx + ox - r, ry + oy - r, rx + ox + r, ry + oy + r), fill=col + (255,))
        d.polygon(_smooth([(rx - 22, ry + 14), (rx - 46, ry + 26), (rx - 20, ry + 30)], 6), fill=(60, 150, 90, 255))
        return im

    def _blush(self):
        im = self.Image.new("RGBA", (C, C + 40), (0, 0, 0, 0))
        d = self.ImageDraw.Draw(im)
        for x in (self.cx - 50, self.cx + 50):
            d.ellipse((x - 24, self.cy + 34, x + 24, self.cy + 54), fill=(255, 110, 150, 110))
        return im.filter(self.ImageFilter.GaussianBlur(8))

    def ring(self, mood: str):
        if mood not in self.rings:
            a, b = (_hex(c) for c in RING[mood])
            im = self.Image.new("RGBA", (C, C), (0, 0, 0, 0))
            d = self.ImageDraw.Draw(im)
            r = C / 2 - 9
            for deg in range(0, 360, 2):  # conic gradient a -> b -> a, fading tail
                t = deg / 360
                col = _mix(a, b, 0.5 - 0.5 * math.cos(2 * math.pi * t))
                alpha = int(90 + 165 * t)
                d.arc((C / 2 - r, C / 2 - r, C / 2 + r, C / 2 + r), deg, deg + 3, fill=col + (alpha,), width=11)
            glow = im.filter(self.ImageFilter.GaussianBlur(9))
            # kept at screen size: rotating the small ring every frame is ~10x cheaper than at 3x
            self.rings[mood] = self.Image.alpha_composite(glow, im).resize((D, D), self.Image.LANCZOS)
        return self.rings[mood]

    # ---- per frame
    def frame(self, mood: str, m: dict, t: float, gaze, openness: float, wink: bool, particles: list):
        Image, ImageDraw = self.Image, self.ImageDraw
        out = self.bg.copy()
        bob = m["bob"] * math.sin(t * 2 * math.pi * 1.6) if m["bob"] else 2 * math.sin(t * 1.3)
        layer = Image.new("RGBA", (C, C), (0, 0, 0, 0))
        layer.alpha_composite(self.char, (0, int(bob)))
        if mood in BLUSHY:
            layer.alpha_composite(self.blush, (0, int(bob)))
        d = ImageDraw.Draw(layer)
        cy = self.cy + bob
        for side in (-1, 1):
            ex = self.cx + side * 33
            op = 0.06 if (wink and side == 1 and m["eye"] != "happy") else openness
            self._eye(d, ex, cy + 8, side, m, op, gaze)
            inner, outer = m["brow"]
            by = cy - 30
            brow = _smooth([(ex - side * 22, by + inner), (ex - side * 2, by - 6 + (inner + outer) / 2),
                            (ex + side * 24, by + 2 + outer)], 8, closed=False)
            d.line(brow, fill=HAIR_D + (255,), width=7)
        self._mouth(d, self.cx, cy + 66, m["mouth"], t)
        # clip the character to the dial
        alpha = layer.getchannel("A")
        from PIL import ImageChops

        layer.putalpha(ImageChops.multiply(alpha, self.inner))
        out.alpha_composite(layer)
        self._extras(ImageDraw.Draw(out), t, m["extra"], particles)
        small = out.resize((D, D), Image.LANCZOS)
        small.alpha_composite(self.ring(mood).rotate(-(t * SPIN.get(mood, 30)) % 360, resample=Image.BICUBIC))
        return small

    def _eye(self, d, ex, ey, side, m, openness, gaze):
        if m["eye"] == "happy":
            d.line(_smooth([(ex - 20, ey + 6), (ex, ey - 10), (ex + 20, ey + 6)], 8, closed=False), fill=LINER,
                   width=7)
            d.line([(ex + side * 20, ey + 4), (ex + side * 30, ey - 6)], fill=LINER, width=5)
            return
        w = 24 if m["eye"] != "wide" else 26
        h = (17 if m["eye"] != "wide" else 21) * openness
        if h < 3:
            d.line(_smooth([(ex - w, ey), (ex, ey + 6), (ex + w, ey)], 8, closed=False), fill=LINER, width=6)
            return
        top = [(ex - w, ey + 2), (ex - w / 2, ey - h), (ex + w / 2, ey - h * 1.05), (ex + w, ey - 1)]
        bot = [(ex + w, ey - 1), (ex + w / 2, ey + h * 0.8), (ex - w / 2, ey + h * 0.85), (ex - w, ey + 2)]
        almond = _smooth(top, 6, closed=False) + _smooth(bot, 6, closed=False)
        d.polygon(almond, fill=(255, 255, 255))
        # iris, clipped to the almond
        gx, gy = gaze
        ix, iy = ex + gx * (w - 12), ey + gy * max(2, h - 9)
        mask = self.Image.new("L", (C, C), 0)
        self.ImageDraw.Draw(mask).polygon(almond, fill=255)
        iris = self.Image.new("RGBA", (C, C), (0, 0, 0, 0))
        di = self.ImageDraw.Draw(iris)
        di.ellipse((ix - 15, iy - 15, ix + 15, iy + 15), fill=IRIS_O + (255,))
        di.ellipse((ix - 11, iy - 11, ix + 11, iy + 11), fill=IRIS_I + (255,))
        di.ellipse((ix - 6, iy - 6, ix + 6, iy + 6), fill=(30, 6, 16, 255))
        di.ellipse((ix - 11, iy - 11, ix - 3, iy - 3), fill=(255, 255, 255, 255))
        di.ellipse((ix + 4, iy + 4, ix + 8, iy + 8), fill=(255, 255, 255, 230))
        iris.putalpha(self._mul(iris.getchannel("A"), mask))
        d._image.alpha_composite(iris)
        # eyeliner with a wing, lower lash line, lashes
        d.line(_smooth(top, 6, closed=False), fill=LINER, width=7)
        d.polygon([(ex + side * w - side * 2, ey - 4), (ex + side * (w + 16), ey - 14), (ex + side * w, ey + 3)],
                  fill=LINER)
        d.line(_smooth(bot, 6, closed=False)[2:-2], fill=(150, 90, 110), width=2)
        for k in (0.25, 0.5, 0.75):
            x = ex - w + 2 * w * k
            y = ey - h * (1.0 if 0.2 < k < 0.8 else 0.9)
            d.line([(x, y), (x + side * 4, y - 7)], fill=LINER, width=3)

    def _mul(self, a, b):
        from PIL import ImageChops

        return ImageChops.multiply(a, b)

    def _mouth(self, d, mx, my, kind, t):
        if kind in ("smile", "small"):
            wdt = 22 if kind == "smile" else 14
            curve = 7 if kind == "smile" else 2
            upper = _smooth([(mx - wdt, my), (mx - 8, my - 7), (mx, my - 4), (mx + 8, my - 7), (mx + wdt, my)], 6,
                            closed=False)
            lower = _smooth([(mx + wdt, my), (mx + 10, my + 9 + curve / 2), (mx - 10, my + 9 + curve / 2),
                             (mx - wdt, my)], 6, closed=False)
            d.polygon(upper + lower, fill=LIP)
            d.line(_smooth([(mx - wdt, my), (mx, my + curve / 2), (mx + wdt, my)], 6, closed=False), fill=LIP_D,
                   width=3)
            d.ellipse((mx - 6, my + 3, mx + 4, my + 6), fill=(255, 150, 175))  # gloss
        elif kind == "grin":
            d.chord((mx - 26, my - 18, mx + 26, my + 22), 0, 180, fill=(90, 14, 38))
            d.chord((mx - 20, my - 10, mx + 20, my + 6), 0, 180, fill=(255, 255, 255))
            d.arc((mx - 26, my - 18, mx + 26, my + 22), 0, 180, fill=LIP, width=6)
            d.line([(mx - 26, my + 2), (mx + 26, my + 2)], fill=LIP, width=5)
        elif kind == "talk":
            h = 6 + 16 * abs(math.sin(t * 13)) * (0.6 + 0.4 * abs(math.sin(t * 3.1)))
            d.ellipse((mx - 16, my - h / 2, mx + 16, my + h / 2 + 4), fill=(90, 14, 38), outline=LIP, width=6)
        elif kind == "o":
            d.ellipse((mx - 10, my - 8, mx + 10, my + 12), fill=(90, 14, 38), outline=LIP, width=6)
        elif kind == "hmm":
            d.line(_smooth([(mx - 18, my + 3), (mx, my + 2), (mx + 18, my - 6)], 6, closed=False), fill=LIP, width=8)
        elif kind == "wavy":
            d.line(_smooth([(mx - 22, my + 3), (mx - 11, my - 4), (mx, my + 3), (mx + 11, my - 4), (mx + 22, my + 3)],
                           6, closed=False), fill=LIP, width=6)

    def _extras(self, d, t, kind, particles):
        for p in particles:
            age = p["age"]
            fade = max(0, min(255, int(255 * (1 - age / 2.2))))
            col = _hex(p["color"]) + (fade,)
            d.text((p["x"] + p["vx"] * age, p["y"] - 60 * age), p["g"], font=self.font, fill=col)
        if kind == "dots":  # thinking bubble
            bx, by = C / 2 + 92, 92
            d.rounded_rectangle((bx - 40, by - 20, bx + 40, by + 20), radius=20, fill=(255, 255, 255, 235))
            for i in range(3):
                up = 5 * max(0.0, math.sin(t * 6 - i * 0.8))
                d.ellipse((bx - 24 + i * 18, by - 6 - up, bx - 12 + i * 18, by + 6 - up), fill=(120, 80, 140, 255))
        elif kind == "sweat":
            sx, sy = C / 2 + 74, 150 + 5 * math.sin(t * 2)
            d.polygon(_smooth([(sx, sy - 14), (sx - 9, sy + 6), (sx, sy + 12), (sx + 9, sy + 6)], 6),
                      fill=(140, 205, 255, 240))


AVATAR_SVG = Path(__file__).resolve().parent / "assets" / "rose_avatar.svg"
AVATAR_HELPER = Path(__file__).resolve().parent / "avatar_render.py"
PROPS_SVG = Path(__file__).resolve().parent / "assets" / "rose_props.svg"
EMOJI_FONT = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"
# tool -> what she holds (same table as the phone app); the big four are drawn props, the rest an emoji in hand
TOOL_PROPS = [
    (r"^webcam_capture$", "camera"), (r"^(take_screenshot|screen_frame|phone_screenshot)$", "tablet"),
    (r"^webcam_live", "binos"), (r"^mic_live|^audio_devices$", "headphones"),
    (r"^(search_files|find_on_screen|ui_read|ui_list|browser_read|read_file|list_dir|github_|observation_stats|"
     r"list_windows)", "🔍"),
    (r"^(type_text|press_keys|ui_fill|browser_fill|clipboard_write|phone_type)$", "⌨️"),
    (r"^(click_on_screen|mouse_click|mouse_move|scroll|ui_click|browser_click|phone_tap|phone_swipe)$", "🖱️"),
    (r"^(run_command|stop_process|system_status|rose_maintenance|quick_settings|set_quick_setting|list_commands|"
     r"ui_enable)", "🔧"),
    (r"^(wifi_|bluetooth_)", "📶"), (r"^(forge_|bridge_)", "✉️"), (r"^(write_file|move_file|open_path|send_to_phone)", "📁"),
    (r"^phone_", "📱"), (r"^(lock_screen|power_control)", "🔒"), (r"^(schedule_|push_|watch_status|send_notification)", "⏰"),
    (r"^(recording_|meeting_transcribe)", "🎤"), (r"^(laptop_say|speak)$", "📢"),
    (r"^(media_control|set_audio_device|set_mic_muted)$", "🎵"), (r"^(open_url|browser_|open_app|list_apps)", "🌐"),
    (r"^motion_", "🚨"), (r"^(focus_window|move_window|arrange_window|tile_windows|close_window|workspace)", "🪟"),
    (r"^(run_routine|list_routines|save_routine)", "🔁"), (r"^undo_", "↩️"), (r"^freellm_", "🤖"),
]
PROP_MOOD = {"camera": "camera", "tablet": "screen", "binos": "lookout", "headphones": "spy"}


def prop_for(tool: str) -> str:
    return next((p for rx, p in TOOL_PROPS if re.search(rx, tool)), "⚙️")
MOUTHS = {"smile": "mouth-smile", "small": "mouth-small", "grin": "mouth-grin", "o": "mouth-o", "hmm": "mouth-hmm",
          "wavy": "mouth-wavy"}


class AvatarPainter(Painter):
    """The anime avatar (rose/assets/rose_avatar.svg): layers rendered once by librsvg (system Python), then
    composed per frame: irises follow the pointer, lids blink, brows/mouth follow the mood, the ahoge sways."""

    def __init__(self):
        super().__init__()
        from PIL import Image

        stamp = f"{int(AVATAR_SVG.stat().st_mtime)}-{int(PROPS_SVG.stat().st_mtime)}-{C}"
        cache = ROSE_HOME / "avatar" / stamp
        if not (cache / "p-flash.png").exists():
            cache.mkdir(parents=True, exist_ok=True)
            subprocess.run(["/usr/bin/python3", str(AVATAR_HELPER), str(AVATAR_SVG), str(cache), str(C),
                            str(PROPS_SVG)], check=True, timeout=60, capture_output=True)
        self._emoji: dict = {}
        self.sp = {}  # layer -> (cropped image, (x, y))
        for f in cache.glob("*.png"):
            im = Image.open(f).convert("RGBA")
            box = im.getbbox() or (0, 0, 1, 1)
            self.sp[f.stem] = (im.crop(box), box[:2])
        self.orig = dict(self.sp)
        self.hue = None
        self._compose_static()

    HUE_LAYERS = ("hair-back", "hair-front", "ahoge", "brow-L", "brow-R", "iris-L", "iris-R")
    BASE_HUE = 332  # the artwork's own rose-pink

    def set_hue(self, hue: float | None) -> None:
        """Hair, brows and eyes take the phone theme's accent hue (lightness and saturation kept)."""
        from PIL import Image

        if hue is None or (self.hue is not None and abs(self.hue - hue) < 1):
            return
        self.hue = hue
        delta = round((hue - self.BASE_HUE) / 360 * 256) % 256
        for k in self.HUE_LAYERS:
            im, pos = self.orig[k]
            alpha = im.getchannel("A")
            h, s, v = im.convert("RGB").convert("HSV").split()
            shifted = h.point(lambda x: (x + delta) % 256)
            h = Image.composite(shifted, h, s.point(lambda x: 255 if x > 30 else 0))  # greys/whites stay
            out = Image.merge("HSV", (h, s, v)).convert("RGB")
            out.putalpha(alpha)
            self.sp[k] = (out, pos)
        self._compose_static()

    def _compose_static(self):
        from PIL import Image

        base = Image.new("RGBA", (C, C + 40), (0, 0, 0, 0))
        for k in ("hair-back", "body", "face"):
            self._put(base, k)
        self.base = base
        front = Image.new("RGBA", (C, C + 40), (0, 0, 0, 0))
        for k in ("hair-front", "rose"):
            self._put(front, k)
        box = front.getbbox()
        self.front = (front.crop(box), box[:2])

    def _put(self, canvas, key, dx=0.0, dy=0.0, sprite=None):
        im, (x, y) = sprite or self.sp[key]
        canvas.alpha_composite(im, (int(round(x + dx)), int(round(y + dy))))

    def frame(self, mood, m, t, gaze, openness, wink, particles, lift: float = 0.0, prop=None, flash=False,
              lip: float | None = None):
        """Just her (transparent around her): the window takes its shape from this image's alpha."""
        from PIL import Image

        bob = (m["bob"] * math.sin(t * 2 * math.pi * 1.6) if m["bob"] else 2 * math.sin(t * 1.3)) - lift * 34
        bob += MARGIN  # room above her head: a jump moves her up without negative offsets
        layer = Image.new("RGBA", (C, C + 40 + MARGIN), (0, 0, 0, 0))
        layer.alpha_composite(self.base, (0, int(bob)))
        if mood in BLUSHY:
            self._put(layer, "blush", 0, bob)
        for side in ("L", "R"):
            op = 0.06 if (wink and side == "R" and m["eye"] != "happy") else openness
            self._eye(layer, side, m, op, gaze, bob)
        mouth = MOUTHS.get(m["mouth"])
        if m["mouth"] == "talk":
            k = lip if lip is not None else abs(math.sin(t * 11)) * (0.6 + 0.4 * abs(math.sin(t * 3.1)))
            mouth = "mouth-talk1" if k < 0.35 else "mouth-talk2" if k < 0.7 else "mouth-talk3"
        if m["mouth"] == "talk":  # her mouth only shows while she talks
            self._put(layer, mouth, 0, bob)
        self._put(layer, "", 0, bob, sprite=self.front)
        inner, outer = m["brow"]  # drawn over the bangs, anime style
        for side, sgn in (("L", -1), ("R", 1)):
            im, (x, y) = self.sp[f"brow-{side}"]
            rot = im.rotate(sgn * (outer - inner) * 0.9, resample=Image.BICUBIC, expand=False)
            layer.alpha_composite(rot, (x, int(y + bob + (inner + outer) / 2)))
        # the ahoge sways (more when she's vibing)
        im, (x, y) = self.sp["ahoge"]
        sway = math.sin(t * (5 if m["bob"] else 1.6)) * (9 if m["bob"] else 4)
        rot = im.rotate(sway, resample=Image.BICUBIC, expand=True, center=(4, im.height - 2))
        layer.alpha_composite(rot, (x - (rot.width - im.width) // 2, int(y + bob - (rot.height - im.height))))
        if m["extra"] == "sweat":
            self._put(layer, "sweat", 0, bob + 3 * math.sin(t * 2))
        if mood == "sad":
            self._put(layer, "p-tear", 0, bob + (t * 22) % 26)
        if prop in ("camera", "tablet", "binos", "headphones"):
            sprite = self.sp[f"p-{prop}"]
            if prop == "binos":  # slow scan
                im, (x, y) = sprite
                sprite = (im.rotate(math.sin(t * 0.9) * 4, resample=Image.BICUBIC, center=(210 - x, 238 - y)), (x, y))
            self._put(layer, "", 0, bob, sprite=sprite)
            if prop == "camera" and flash:
                self._put(layer, "p-flash", 0, bob)
        elif prop:
            dy = bob + 2 * math.sin(t * 3)
            self._put(layer, "p-item", 0, dy)
            em = self._emoji_img(prop)
            if em is not None:
                layer.alpha_composite(em, (286 - em.width // 2, int(264 - em.height // 2 + dy)))
        self._extras(self.ImageDraw.Draw(layer), t, m["extra"] if m["extra"] != "sweat" else None, particles)
        x0, y0, x1, y1 = CROP
        out = layer.crop((x0, y0 + MARGIN - 30, x1, y1 + MARGIN - 30)).resize((OW, OH), Image.LANCZOS)
        if not hasattr(self, "_bust"):  # round the bottom of the bust (no hard cut at the shoulders)
            from PIL import ImageDraw

            self._bust = Image.new("L", (OW, OH), 0)
            ImageDraw.Draw(self._bust).ellipse((-24, -OH, OW + 24, OH - 1), fill=255)
        from PIL import ImageChops

        out.putalpha(ImageChops.multiply(out.getchannel("A"), self._bust))
        return out

    def _emoji_img(self, ch: str):
        """A colour emoji at ~34 px (420 space): Noto Color Emoji only renders at 109 px, so scale it."""
        if ch not in self._emoji:
            try:
                from PIL import Image, ImageDraw, ImageFont

                font = ImageFont.truetype(EMOJI_FONT, 109)
                im = Image.new("RGBA", (140, 140), (0, 0, 0, 0))
                ImageDraw.Draw(im).text((70, 70), ch, font=font, embedded_color=True, anchor="mm")
                box = im.getbbox() or (0, 0, 140, 140)
                self._emoji[ch] = im.crop(box).resize((34, 34), Image.LANCZOS)
            except Exception:
                self._emoji[ch] = None
        return self._emoji[ch]

    def _eye(self, layer, side, m, openness, gaze, bob):
        from PIL import Image, ImageChops

        if m["eye"] == "happy":
            self._put(layer, f"happy-{side}", 0, bob)
            return
        if openness < 0.14:
            self._put(layer, f"closed-{side}", 0, bob)
            return
        white, (wx, wy) = self.sp[f"eye-{side}-white"]
        iris, (ix, iy) = self.sp[f"iris-{side}"]
        w, h = white.size
        eye = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        eye.alpha_composite(white)
        gx, gy = gaze
        eye.alpha_composite(iris, (int(ix - wx + gx * 9), int(iy - wy + gy * 6)))
        mask = white.getchannel("A")
        cut = int(h * (1 - openness))  # the upper lid comes down
        if cut > 0:
            lid = Image.new("L", (w, h), 255)
            lid.paste(0, (0, 0, w, cut))
            mask = ImageChops.multiply(mask, lid)
        eye.putalpha(ImageChops.multiply(eye.getchannel("A"), mask))
        layer.alpha_composite(eye, (wx, int(wy + bob)))
        self._put(layer, f"lash-{side}", 0, bob + cut * 0.92)


# ---------------------------------------------------------------- X11 helpers

def _x_top(disp, wid: int):
    """The real top-level X window: on a desktop with a window manager Tk wraps its toplevel in an outer
    window, and only shaping that one removes the box around her."""
    win = disp.create_resource_object("window", wid)
    root = disp.screen().root.id
    while True:
        parent = win.query_tree().parent
        if not parent or parent.id in (0, root):
            return win
        win = parent


def _x_shape(widget, w: int, h: int, radius: int | None) -> None:
    """Round the window: a circle (radius=None) or rounded corners. Needs the X Shape extension."""
    try:
        import Xlib.display
        from Xlib.ext import shape

        disp = Xlib.display.Display()
        win = _x_top(disp, int(widget.wm_frame(), 16))
        pm = win.create_pixmap(w, h, 1)
        gc = pm.create_gc(foreground=0, background=0)
        pm.fill_rectangle(gc, 0, 0, w, h)
        gc.change(foreground=1)
        if radius is None:
            pm.fill_arc(gc, 0, 0, w, h, 0, 360 * 64)
        else:
            r = radius
            pm.fill_rectangle(gc, r, 0, w - 2 * r, h)
            pm.fill_rectangle(gc, 0, r, w, h - 2 * r)
            for x, y in ((0, 0), (w - 2 * r, 0), (0, h - 2 * r), (w - 2 * r, h - 2 * r)):
                pm.fill_arc(gc, x, y, 2 * r, 2 * r, 0, 360 * 64)
        win.shape_mask(shape.SO.Set, shape.SK.Bounding, 0, 0, pm)
        disp.sync()
        disp.close()
    except Exception as e:  # no Shape extension: square corners, still works
        try:
            with open(ROSE_HOME / "companion.log", "a") as f:
                f.write(f"{time.strftime('%F %T')} shape failed: {type(e).__name__}: {e}\n")
        except OSError:
            pass


def _x_undecorated(widget) -> None:
    """No title bar, but still a normal managed window (so it can take keyboard focus)."""
    try:
        import Xlib.display

        disp = Xlib.display.Display()
        win = disp.create_resource_object("window", int(widget.wm_frame(), 16))
        atom = disp.intern_atom("_MOTIF_WM_HINTS")
        win.change_property(atom, atom, 32, [2, 0, 0, 0, 0])
        disp.sync()
        disp.close()
    except Exception:
        pass


# ---------------------------------------------------------------- windows

class ChatBox(tk.Toplevel):
    """Small rounded chat card next to the dial. A managed window: the entry really gets the keyboard."""

    def __init__(self, app: Companion):
        super().__init__(app)
        self.app = app
        self.withdraw()
        self.configure(bg=BG)
        self.title("Rose chat")
        self.attributes("-topmost", True)
        try:
            self.attributes("-type", "dialog")
        except tk.TclError:
            pass
        self.geometry(f"{POP_W}x{POP_H}")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.hide)

        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=14, pady=(10, 2))
        tk.Label(head, text="Rose", bg=BG, fg=TEXT, font=("DejaVu Sans", 11, "bold")).pack(side="left")
        self.status = tk.Label(head, text="", bg=BG, fg=MUTED, font=("DejaVu Sans", 8))
        self.status.pack(side="left", padx=8)
        close = tk.Label(head, text="✕", bg=BG, fg=MUTED, font=("DejaVu Sans", 10), cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.hide())

        self.text = tk.Text(self, height=5, wrap="word", bg=CARD, fg=TEXT, relief="flat", bd=0,
                            font=("DejaVu Sans", 10), padx=10, pady=8, highlightthickness=0, cursor="arrow")
        self.text.pack(fill="both", expand=True, padx=12)
        self.set_text("Hey. Ask me anything, or tell me what to do on the laptop.")

        self.approve = tk.Label(self, text="Approve in browser →", bg=ACCENT, fg="white",
                                font=("DejaVu Sans", 9, "bold"), cursor="hand2", pady=4)
        self.approve.bind("<Button-1>", lambda e: webbrowser.open(self.app.approval))

        self.row = tk.Frame(self, bg=FIELD)
        self.row.pack(fill="x", padx=12, pady=10)
        self.entry = tk.Entry(self.row, bg=FIELD, fg=TEXT, insertbackground=ACCENT, relief="flat", bd=0,
                              font=("DejaVu Sans", 11), highlightthickness=0)
        self.entry.pack(side="left", fill="x", expand=True, padx=(12, 6), ipady=7)
        self.entry.bind("<Return>", lambda e: self.app.send())
        self.entry.bind("<Escape>", lambda e: self.hide())
        send = tk.Label(self.row, text="➤", bg=ACCENT, fg="white", font=("DejaVu Sans", 11, "bold"), width=3,
                        cursor="hand2")
        send.pack(side="right", padx=4, pady=4, ipady=3)
        send.bind("<Button-1>", lambda e: self.app.send())
        self.shaped = False

    def set_text(self, s: str):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", s)
        self.text.see("end")
        self.text.configure(state="disabled")

    def place_near(self):
        a = self.app
        ax, ay = a.winfo_x(), a.winfo_y()
        sw = a.winfo_screenwidth()
        x = ax - POP_W - 8 if ax - POP_W - 8 > 0 else min(ax + a.W + 8, sw - POP_W)
        y = max(0, ay + a.H - POP_H)
        self.geometry(f"{POP_W}x{POP_H}+{x}+{y}")

    def show(self, focus: bool = True):
        self.place_near()
        self.deiconify()
        self.lift()
        if not self.shaped:
            self.update_idletasks()
            _x_undecorated(self)
            _x_shape(self, POP_W, POP_H, 18)
            self.shaped = True
        if focus:
            self.after(60, self._focus)

    def _focus(self):
        try:
            self.focus_force()
            self.entry.focus_set()
        except tk.TclError:
            pass

    def hide(self):
        self.withdraw()

    def visible(self) -> bool:
        return self.state() == "normal"


class Bubble(tk.Toplevel):
    """A small speech bubble above her head for news (Claude needs you / finished, files sent). Never takes
    the keyboard; click to dismiss."""

    def __init__(self, app: Companion):
        super().__init__(app)
        self.app = app
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=ACCENT)
        self.label = tk.Label(self, text="", bg=CARD, fg=TEXT, font=("DejaVu Sans", 9), wraplength=220,
                              justify="left", padx=10, pady=6)
        self.label.pack(padx=1, pady=1)
        self.label.bind("<Button-1>", lambda e: self.hide())
        self.hide_at = 0.0
        self._job = None

    def show(self, text: str, secs: float = 6):
        self.label.configure(text=text)
        self.update_idletasks()
        a = self.app
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        sw = a.winfo_screenwidth()
        x = min(max(0, a.winfo_x() + a.W // 2 - w // 2), sw - w)
        y = a.winfo_y() - h - 6
        if y < 0:
            y = a.winfo_y() + a.H + 6
        self.geometry(f"+{x}+{y}")
        self.deiconify()
        self.lift()
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(int(secs * 1000), self.hide)

    def hide(self):
        self._job = None
        self.withdraw()


class Companion(tk.Tk):
    def __init__(self):
        super().__init__()
        from PIL import ImageTk

        self.ImageTk = ImageTk
        self.title("Rose")
        self.configure(bg=BG)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        st = _state()

        self.events: queue.Queue = queue.Queue()
        self.ctx_mood, self.status = "idle", "hanging out"
        self.mood = "idle"
        self.busy = False
        self.reply = ""
        self.talk_until = 0.0
        self.speak_on = bool(st.get("speak", False))
        self.approval = ""
        self.t0 = time.monotonic()
        self.blink_at, self.blink_t = time.monotonic() + 2, -1.0
        self.wink_at = time.monotonic() + random.uniform(20, 50)
        self.glance_until, self.glance = 0.0, (0.0, 0.0)
        self.gaze = [0.0, 0.0]
        self.last_ptr, self.ptr_still_since = (0, 0), time.monotonic()
        self.particles: list[dict] = []

        try:
            self.painter = AvatarPainter()
        except Exception:  # no librsvg/system Python: the simpler drawn face in a round dial
            self.painter = Painter()
        self.cutout = isinstance(self.painter, AvatarPainter)
        self.W, self.H = (OW, OH) if self.cutout else (D, D)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x, y = st.get("pos", (sw - self.W - 30, sh - self.H - 70))
        self.geometry(f"{self.W}x{self.H}+{int(x)}+{int(y)}")
        self.jump_at = -10.0
        self.tool = None  # {"name", "at", "done"}: the tool Rose is using (drives her props)
        self.lips = None  # {"start", "levels"}: loudness of the voice playing now (lip sync)
        self.barge = None  # wake.BargeIn while her voice plays: the user talking over her pauses her
        self.barge_paused_at = 0.0
        self.listen_until = 0.0  # after "Hey Rose", until the command is heard
        self.proud_at = self.sad_at = self.surprised_at = self.love_at = -1e9
        self._x = None  # (display, window) for shape updates
        self._mask = b""
        if self.cutout:
            self._masks = self._silhouettes()
        self.label = tk.Label(self, bg=BG, bd=0, highlightthickness=0)
        self.label.pack()
        self.photo = None
        self.chatbox = ChatBox(self)
        self._menu_build()
        self.label.bind("<ButtonPress-1>", self._press)
        self.label.bind("<B1-Motion>", self._motion)
        self.label.bind("<ButtonRelease-1>", self._release)
        self.label.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        if not self.cutout:
            for ms in (150, 1000, 3000):  # again once mapped: the first try can come before the window exists
                self.after(ms, lambda: _x_shape(self, D, D, None))
        threading.Thread(target=self._context_loop, daemon=True).start()
        self.voice_turn = False
        self.wake = None
        self.ptt = None  # {"stop": Event, "gen": int} while push-to-talk records/transcribes
        self.ptt_gen = 0
        self.sys = None  # Claude Code / CI state (read_system_state)
        self.sys_proud_until = self.worried_at = -1e9
        self.bubble = Bubble(self)
        self.hidden = False  # closed to the background: windows away, wake word and hotkey still running
        if _state().get("wake", True):
            self._wake_start()
        self.hotkey = None
        self.hotkey_label = ""
        self._hotkey_start()
        if _state().get("talk_on_start"):  # started by `rose face --talk`
            _save_state(talk_on_start=False)
            self.after(1200, lambda: self.events.put(("hotkey", None)))
        self.after(800, self._wheel_start)
        self.after(int(1000 / FPS), self._tick)

    # ---- the menu wheel (desktop/wheel: the phone app's quick-action dial, its own movable window)
    def _wheel_start(self):
        if _state().get("wheel", True):
            start_wheel()

    def _toggle_wheel(self):
        on = self.wheel_var.get()
        _save_state(wheel=on)
        if on:
            start_wheel()
        else:
            stop_wheel()

    def destroy(self):
        stop_wheel()  # the wheel comes and goes with Rose's face
        super().destroy()

    # ---- close to the background (she keeps listening) / come back
    def hide_to_background(self):
        """Closing Rose only puts her away: this process stays alive, so "Hey Rose", the hotkey and
        `rose face` can bring her straight back."""
        _save_state(hidden=True)
        if self.hidden:
            return
        self.hidden = True
        self.chatbox.hide()
        self.bubble.hide()
        self.withdraw()
        stop_wheel()

    def show_from_background(self):
        _save_state(hidden=False)
        if not self.hidden:
            return
        self.hidden = False
        self.deiconify()
        self.lift()
        self.jump_at = time.monotonic()
        self._wheel_start()

    def apply_visibility(self):
        """SIGUSR2: follow companion.json "hidden" (set by `rose face` / `rose face --hide`)."""
        (self.hide_to_background if _state().get("hidden") else self.show_from_background)()

    # ---- menu / drag / click
    def _menu_build(self):
        self.menu = tk.Menu(self, tearoff=0, bg=CARD, fg=TEXT, activebackground=ACCENT, activeforeground="white",
                            bd=0)
        self.menu.add_command(label="Chat with Rose", command=lambda: self.chatbox.show())
        self.menu.add_command(label="Send clipboard file to phone", command=lambda: self._send_files("clipboard"))
        self.menu.add_command(label="Send latest download to phone", command=lambda: self._send_files("download"))
        self.speak_var = tk.BooleanVar(value=self.speak_on)
        self.menu.add_checkbutton(label="Speak replies", variable=self.speak_var, command=self._toggle_speak)
        self.cave_var = tk.BooleanVar(value=_state().get("caveman", True))
        self.menu.add_checkbutton(label="Caveman mode (short replies)", variable=self.cave_var,
                                  command=lambda: _save_state(caveman=self.cave_var.get()))
        from rose import wake

        self.wake_var = tk.BooleanVar(value=_state().get("wake", True) and wake.available())
        self.menu.add_checkbutton(label='Listen for "Hey Rose" (offline)', variable=self.wake_var,
                                  command=self._toggle_wake)
        self.barge_var = tk.BooleanVar(value=barge_wanted())
        self.menu.add_checkbutton(label="Stop talking when I talk", variable=self.barge_var,
                                  command=lambda: _save_state(barge=self.barge_var.get()))
        self.menu.add_command(label="New conversation", command=self._new_chat)
        self.wheel_var = tk.BooleanVar(value=_state().get("wheel", True))
        self.menu.add_checkbutton(label="Menu wheel", variable=self.wheel_var, command=self._toggle_wheel)
        self.wheel_btn_var = tk.BooleanVar(value=wheel_button_shown())
        self.menu.add_checkbutton(label="Wheel button (Super+C opens the wheel)", variable=self.wheel_btn_var,
                                  command=lambda: set_wheel_button(self.wheel_btn_var.get()))
        self.menu.add_separator()
        self.menu.add_command(label="Restart Rose", command=self._restart)
        self.menu.add_command(label="Hide Rose (keeps listening)", command=self.hide_to_background)
        self.menu.add_command(label="Quit Rose (stops listening)", command=self.destroy)

    def _restart(self):
        PID_FILE.unlink(missing_ok=True)
        self.destroy()
        start_detached()

    # ---- "Hey Rose" (offline wake word, rose/wake.py)
    def _wake_start(self):
        from rose import wake

        if not wake.available() or self.wake:
            return
        self.wake = wake.WakeListener(
            on_wake=lambda: self.events.put(("wake", None)),
            on_command=lambda wav: threading.Thread(target=self._transcribe, args=(wav,), daemon=True).start(),
            paused=lambda: self.busy or self.ptt is not None or time.monotonic() < self.talk_until + 0.6,
            on_mic=lambda n, err: (_log(f"mic went away ({n}x){f': {err}' if err else ''}; waiting for it to "
                                        "come back") if n <= 3 or n % 60 == 0 else None))
        self.wake.start()

    def _wake_guard(self):
        """Her ears must outlive everything, above all while she is closed away and nobody would notice: if
        the wake thread ever died (a Vosk or audio error), start listening again. The mic itself comes back
        on its own (rose.wake.MicStream)."""
        if not _state().get("wake", True):
            return
        if self.wake is not None and not self.wake.is_alive():
            _log("wake word: the listener stopped; starting it again")
            self.wake = None
        if self.wake is None:
            self._wake_start()

    # ---- talking over her (barge-in): she pauses, listens, then takes the new command or carries on
    def _barge_tick(self):
        want = speaking() and self.ptt is None and self.barge_var.get()
        cur = self.barge
        if want and cur is None:
            from rose import wake

            self.barge = wake.BargeIn(on_start=self._barge_start, on_speech=self._barge_speech)
            self.barge.start()
        elif not want and cur is not None and not cur.triggered:
            cur.stop()
            self.barge = None

    def _barge_start(self):
        pause_speech()
        self.events.put(("barge", None))

    def _barge_speech(self, wav: bytes):
        from rose import wake

        text = transcribe(wav)
        self.events.put(("barge_text", wake.strip_wake(text).strip() if text else ""))

    def _wake_tick(self):
        """_tick runs many times a second; her ears only need looking at every 10 s."""
        now = time.monotonic()
        if now - getattr(self, "_wake_checked", 0) < 10:
            return
        self._wake_checked = now
        self._wake_guard()

    def _toggle_wake(self):
        on = self.wake_var.get()
        _save_state(wake=on)
        if on:
            self._wake_start()
        elif self.wake:
            self.wake.stop()
            self.wake = None

    def _transcribe(self, wav: bytes):
        from rose import wake

        text = transcribe(wav)
        self.events.put(("voice_cmd", wake.strip_wake(text) if text is not None else None))

    # ---- global hotkey: push-to-talk (rose/hotkey.py; `rose face --talk` does the same)
    def _hotkey_start(self):
        spec = hotkey_spec()
        if not spec:
            return
        self.hotkey_label = "+".join(p.capitalize() for p in spec.split("+"))
        try:
            from rose import hotkey

            hotkey.parse(spec)
            self.hotkey = hotkey.HotkeyGrabber(spec, on_press=lambda: self.events.put(("hotkey", None)),
                                               on_error=lambda m: self.events.put(("hotkey_error", m)))
            self.hotkey.start()
        except Exception as e:
            self.events.put(("hotkey_error", f"hotkey {spec}: {type(e).__name__}: {e}"))
            return
        if "super" in spec.split("+") and gnome_keeps_super():
            self.events.put(("hotkey_error", f"GNOME keeps the Super key for its overview, so {self.hotkey_label} "
                             "never reaches Rose. Add a GNOME keyboard shortcut for it that runs: rose face --talk "
                             "(or set face_hotkey to e.g. \"ctrl+alt+r\")."))

    def _hotkey_error(self, msg: str):
        _log(msg)
        if _state().get("hotkey_warned") != msg:  # one notification per distinct problem
            _save_state(hotkey_warned=msg)
            threading.Thread(target=_notify, args=("Rose hotkey", msg), daemon=True).start()

    def _hotkey(self):
        cb = self.chatbox
        now = time.monotonic()
        if self.ptt:  # pressed again: cancel
            self.ptt["stop"].set()
            self.ptt = None
            self.listen_until = 0
            cb.set_text("Cancelled.")
            return
        self.show_from_background()
        if self.state() != "normal":
            self.deiconify()
        self.lift()
        self.jump_at = now
        cb.show(focus=True)
        self.ptt_gen += 1
        self.ptt = {"stop": threading.Event(), "gen": self.ptt_gen}
        self.listen_until = now + 24
        again = f"{self.hotkey_label} again" if self.hotkey_label else "the hotkey again"
        cb.set_text(f"🎙️ Listening… (pause to send, {again} to cancel)")
        threading.Thread(target=self._ptt_run, args=(self.ptt["stop"], self.ptt_gen), daemon=True).start()

    def _ptt_run(self, stop: threading.Event, gen: int):
        from rose import wake

        try:
            wav = wake.push_to_talk(stop)
        except Exception as e:
            _log(f"push-to-talk failed: {type(e).__name__}: {e}")
            self.events.put(("ptt_text", (gen, None)))
            return
        if stop.is_set():
            return
        self.events.put(("ptt_heard", gen))
        self.events.put(("ptt_text", (gen, transcribe(wav))))

    def _ptt_text(self, gen: int, text: str | None):
        cb = self.chatbox
        if not self.ptt or self.ptt["gen"] != gen:  # cancelled meanwhile
            return
        self.ptt = None
        self.listen_until = 0
        text = (text or "").strip() if text is not None else None
        if text:
            cb.entry.delete(0, "end")
            cb.entry.insert(0, text)
            if self.busy:
                cb.set_text((self.reply + "\n\n" if self.reply else "") + "Still on the last one: your words are "
                            "in the box, press Enter when I'm done.")
            else:
                self.send(spoken=text)
        elif text == "":
            cb.set_text("I didn't catch anything. Try again?")
        else:
            cb.set_text("Couldn't reach Whisper for that one. Is rose-phone running?")

    # ---- files to the phone
    def _send_files(self, source: str):
        self.bubble.show("Sending to your phone…", 4)
        threading.Thread(target=send_files, args=(source, self.events), daemon=True).start()

    # ---- Claude Code / CI state (read in the context thread)
    def _apply_sys(self, cur: dict):
        now = time.monotonic()
        news = bubble_for(self.sys, cur)
        self.sys = cur
        if news:
            mood, text = news
            if mood == "surprised":
                self.surprised_at = now
            else:
                self.sys_proud_until = now + 4
            self.jump_at = now
            self.bubble.show(text, 10 if mood == "surprised" else 7)
        if cur["claude"] == "waiting" and now - self.surprised_at > 15:
            self.surprised_at = now  # still waiting: remind now and then
        if cur["ci_failing"] and now - self.worried_at > 75:
            self.worried_at = now

    def _toggle_speak(self):
        self.speak_on = self.speak_var.get()
        _save_state(speak=self.speak_on)

    def _new_chat(self):
        _save_state(conversation_id=None, fresh=True)
        self.chatbox.set_text("New conversation. What's up?")
        self.chatbox.show()

    def _press(self, e):
        self._drag = (e.x_root - self.winfo_x(), e.y_root - self.winfo_y(), e.x_root, e.y_root)

    def _motion(self, e):
        self.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")
        if self.chatbox.visible():
            self.chatbox.place_near()

    def _release(self, e):
        moved = abs(e.x_root - self._drag[2]) + abs(e.y_root - self._drag[3]) > 6
        if moved:
            _save_state(pos=(self.winfo_x(), self.winfo_y()))
        elif self.chatbox.visible():
            self.chatbox.hide()
        else:
            self.say_hi()

    def say_hi(self):
        now = time.monotonic()
        self.jump_at = now
        self.talk_until = now + 1.1
        if not self.busy:
            self.chatbox.set_text("Hii! 👋 What's up?")
        self.chatbox.show()
        if self.speak_on:
            threading.Thread(target=speak, args=("Hii!", self.events), daemon=True).start()

    # ---- chat
    def send(self, spoken: str | None = None):
        text = (spoken if spoken is not None else self.chatbox.entry.get()).strip()
        if not text or self.busy:
            return
        self.voice_turn = spoken is not None  # a spoken command gets a spoken answer
        self.chatbox.entry.delete(0, "end")
        if re.search(r"\b(thanks?|thank you|thx|ty|love (you|u|it)|good (girl|job)|well done|awesome|amazing)\b|❤",
                     text, re.I):
            self.love_at = time.monotonic()
        self.busy, self.reply = True, ""
        self.chatbox.approve.pack_forget()
        self.chatbox.set_text(f"You: {text}\n\n…")
        threading.Thread(target=chat, args=(text, self.events), daemon=True).start()

    def _drain_events(self):
        cb = self.chatbox
        while True:
            try:
                kind, val = self.events.get_nowait()
            except queue.Empty:
                return
            if kind == "ctx":
                self.ctx_mood, self.status = val
            elif kind == "sys":
                self._apply_sys(val)
            elif kind == "hotkey":
                self._hotkey()
            elif kind == "hotkey_error":
                self._hotkey_error(val)
            elif kind == "ptt_heard":
                if self.ptt and self.ptt["gen"] == val:
                    self.listen_until = 0
                    cb.set_text("✍️ Got it, transcribing…")
            elif kind == "ptt_text":
                self._ptt_text(*val)
            elif kind == "notice":
                text, ok = val
                self.bubble.show(text, 6)
                if ok:
                    self.proud_at = time.monotonic()
                else:
                    self.sad_at = time.monotonic()
            elif kind == "theme":
                if hasattr(self.painter, "set_hue"):
                    self.painter.set_hue(val)
            elif kind == "delta":
                if not self.reply:
                    self.jump_at = time.monotonic()  # she pops up to answer
                self.reply += val
                cb.set_text(self.reply)
                self.talk_until = time.monotonic() + 0.8
            elif kind == "tool":
                self.tool = {"name": val, "at": time.monotonic(), "done": 0.0}
                self.status = f"using {val}…"
                cb.set_text((self.reply + "\n" if self.reply else "") + f"⚙ {val}…")
            elif kind == "wake":  # "Hey Rose": she hops up and listens (even if she was closed away)
                self.show_from_background()
                self.jump_at = time.monotonic()
                self.listen_until = time.monotonic() + 10
                cb.set_text("🎙️ Listening…")
                if not cb.visible():
                    cb.show(focus=False)
            elif kind == "voice_cmd":
                self.listen_until = 0
                if val:
                    self.send(spoken=val)
                else:
                    cb.set_text("Yes? I didn't catch that. Say \"Hey Rose\" again." if val == "" else
                                "Couldn't reach Whisper for that one.")
            elif kind == "barge":  # the user started talking over her: she pauses and listens
                now = time.monotonic()
                self.barge_paused_at = now
                self.listen_until = now + 20
                cb.set_text("🎙️ Listening…")
                if not cb.visible():
                    cb.show(focus=False)
            elif kind == "barge_text":
                self.barge = None
                self.listen_until = 0
                now = time.monotonic()
                if val:  # a new command: her old reply is dropped
                    stop_speech()
                    self.lips, self.talk_until = None, 0
                    if self.busy:
                        cb.entry.delete(0, "end")
                        cb.entry.insert(0, val)
                    else:
                        self.send(spoken=val)
                else:  # nothing worth sending: she carries on where she was
                    held = now - self.barge_paused_at if self.barge_paused_at else 0
                    if self.lips:
                        self.lips["start"] += held
                    self.talk_until += held
                    cb.set_text(self.reply or "")
                    resume_speech()
                self.barge_paused_at = 0.0
            elif kind == "lips":  # playback starts now: mouth follows the voice, she talks until it ends
                self.lips = {"start": time.monotonic(), "levels": val}
                self.talk_until = time.monotonic() + len(val) * 0.04 + 0.2
            elif kind == "tool_done":
                now = time.monotonic()
                if self.tool:
                    self.tool["done"] = now
                if val:
                    self.proud_at = now
                else:
                    self.sad_at = now
            elif kind == "approval":
                self.surprised_at = time.monotonic()
                self.approval = val
                cb.approve.pack(fill="x", padx=12, before=cb.row)
            elif kind == "error":
                cb.set_text(val)
            elif kind == "done":
                self.busy = False
                if self.reply and (self.speak_on or self.voice_turn):
                    reply = self.reply
                    self.talk_until = time.monotonic() + min(20, 0.06 * len(reply) + 1)
                    threading.Thread(target=speak, args=(reply, self.events), daemon=True).start()

    def _context_loop(self):
        n = 0
        while True:
            try:
                self.events.put(("ctx", mood_for(screen_context(), time.monotonic() - self.ptr_still_since)))
            except Exception:
                pass
            try:
                self.events.put(("sys", read_system_state()))
            except Exception:
                pass
            if n % 10 == 0:  # every 20 s: the phone app's theme colour
                hue = theme_hue()
                if hue is not None:
                    self.events.put(("theme", hue))
            n += 1
            time.sleep(2)

    # ---- animation
    def _active_prop(self, now: float):
        t = self.tool
        if t and (not t["done"] or now - t["done"] < 2.6):
            return prop_for(t["name"])
        return None

    def _current_mood(self, now: float) -> str:
        if now < self.listen_until:
            return "listening"
        if now - self.surprised_at < 2.6:
            return "surprised"
        if now < self.sys_proud_until and not self.busy:
            return "proud"
        if now - self.love_at < 3.2:
            return "love"
        if now - self.sad_at < 4.5:
            return "sad"
        prop = self._active_prop(now)
        if prop in PROP_MOOD and now >= self.talk_until:
            return PROP_MOOD[prop]
        if now - self.proud_at < 1.3 and not self.busy:
            return "proud"
        if self.busy and not self.reply:
            return "working"
        if now < self.talk_until:
            return "talking"
        if self.chatbox.visible() and self.chatbox.entry.get():
            return "listening"
        if now - self.worried_at < 4:
            return "worried"  # CI is failing (now and then)
        if self.sys and self.sys["claude"] == "busy":
            return "working"  # Claude Code is working
        return self.ctx_mood

    def _tick(self):
        if self.hidden:  # closed to the background: nothing to draw, but she keeps listening
            self._drain_events()
            self._wake_tick()
            self._barge_tick()
            self.after(200, self._tick)
            return
        try:
            self._drain_events()
            self._wake_tick()
            self._barge_tick()
            now = time.monotonic()
            if now - getattr(self, "_raised_at", 0) > 2:  # frameless windows drop behind newly raised apps
                self._raised_at = now
                self.lift()
            self.mood = self._current_mood(now)
            m = MOODS[self.mood]
            if self.chatbox.visible():
                label = self.status if self.mood == self.ctx_mood else m["label"]
                if self.chatbox.status.cget("text") != label:
                    self.chatbox.status.configure(text=label)
            self._update_gaze(now, m)
            self._spawn(now, m["extra"])
            blink = self._blink(now)
            wink = False
            if now >= self.wink_at:
                wink = now - self.wink_at < 0.35
                if not wink:
                    self.wink_at = now + random.uniform(25, 60)
            if self.cutout:
                p = (now - self.jump_at) / 0.55
                lift = math.sin(math.pi * p) if 0 <= p < 1 else 0.0  # a quick hop
                t = self.tool
                flash = bool(t and t["done"] and now - t["done"] < 0.26)
                lip = None
                if self.lips:
                    i = int((now - self.lips["start"]) / 0.04)
                    lip = self.lips["levels"][i] if 0 <= i < len(self.lips["levels"]) else None
                img = self.painter.frame(self.mood, m, now - self.t0, tuple(self.gaze), m["open"] * blink, wink,
                                         self.particles, lift, self._active_prop(now), flash, lip)
                self._shape_to(self._masks[1] if 0 <= p < 1 else self._masks[0])
            else:
                img = self.painter.frame(self.mood, m, now - self.t0, tuple(self.gaze), m["open"] * blink, wink,
                                         self.particles)
            self.photo = self.ImageTk.PhotoImage(img)
            self.label.configure(image=self.photo)
        finally:  # full frame rate only while something moves a lot; calm idle at 12 fps saves CPU
            now = time.monotonic()
            busy = (self.mood in ("talking", "vibing", "focus_music", "working", "listening") or self.particles
                    or now - self.jump_at < 0.6 or self._active_prop(now) is not None) if self.cutout else True
            self.after(int(1000 / (FPS if busy else 12)), self._tick)

    def _silhouettes(self):
        """Two fixed window outlines: everything she covers while idle/bobbing/swaying, and during a jump.
        Re-shaping on every frame made the compositor show stale strips; these change only when she hops."""
        from PIL import ImageChops

        def union(frames):
            acc = None
            for im in frames:
                a = im.getchannel("A").point(lambda v: 255 if v >= 110 else 0)
                acc = a if acc is None else ImageChops.lighter(acc, a)
            return acc

        p, normal, jump = self.painter, [], []
        for i in range(24):
            t = i / 24 * 1.25
            for mood in ("idle", "vibing"):
                normal.append(p.frame(mood, MOODS[mood], t, (0, 0), 1.0, False, [], 0.0))
            jump.append(p.frame("talking", MOODS["talking"], t, (0, 0), 1.0, False, [], math.sin(math.pi * i / 23)))
        n = union(normal)
        return n, ImageChops.lighter(n, union(jump))

    def _shape_to(self, mask):
        """Make the window her silhouette (no box, no border): X Shape mask. Only sent when it changes."""
        if not self.winfo_ismapped():
            return
        img = mask.convert("RGBA")
        img.putalpha(mask)
        bits = mask.convert("1").tobytes()
        if bits == self._mask:
            return
        self._mask = bits
        try:
            import Xlib.display
            from Xlib import X
            from Xlib.ext import shape

            if self._x is None:
                disp = Xlib.display.Display()
                self._x = (disp, _x_top(disp, int(self.wm_frame(), 16)))
            disp, win = self._x
            w, h = img.size
            row = (w + 7) // 8
            pad = disp.display.info.bitmap_format_scanline_pad // 8
            stride = (row + pad - 1) // pad * pad
            lsb = disp.display.info.bitmap_format_bit_order == X.LSBFirst
            data = bytearray()
            for y in range(h):
                line = bits[y * row:(y + 1) * row]
                data += (line.translate(_REVERSE) if lsb else line) + bytes(stride - row)
            pm = win.create_pixmap(w, h, 1)
            gc = pm.create_gc(foreground=1, background=0)
            pm.put_image(gc, 0, 0, w, h, X.ZPixmap, 1, 0, bytes(data))
            win.shape_mask(shape.SO.Set, shape.SK.Bounding, 0, 0, pm)
            gc.free()
            pm.free()
            disp.flush()
        except Exception as e:
            self._mask = b""
            try:
                with open(ROSE_HOME / "companion.log", "a") as f:
                    f.write(f"{time.strftime('%F %T')} silhouette shape failed: {type(e).__name__}: {e}\n")
            except OSError:
                pass

    def _spawn(self, now: float, kind: str | None):
        rate = {"notes": 0.07, "z": 0.03, "sparkle": 0.04}.get(kind or "", 0)
        if rate and random.random() < rate:
            g, col = {"notes": (random.choice("♪♫♬"), random.choice(("#ff4f8b", "#ffb347", "#ffffff"))),
                      "z": ("z", "#9fb3ff"), "sparkle": ("✦", "#ffd36e")}[kind]
            self.particles.append({"g": g, "color": col, "x": C / 2 + random.choice((-1, 1)) * random.uniform(80, 130),
                                   "y": 150, "vx": random.uniform(-14, 14), "born": now})
        alive = []
        for p in self.particles:
            p["age"] = now - p["born"]
            if p["age"] < 2.2:
                alive.append(p)
        self.particles = alive[-12:]

    def _update_gaze(self, now: float, m: dict):
        px, py = self.winfo_pointerxy()
        if (px, py) != self.last_ptr:
            self.last_ptr, self.ptr_still_since = (px, py), now
        cx = self.winfo_rootx() + self.W / 2
        cy = self.winfo_rooty() + self.H * 0.45
        dx, dy = px - cx, py - cy
        dist = math.hypot(dx, dy) or 1
        reach = min(1.0, dist / 300)
        target = (dx / dist * reach, dy / dist * reach)
        g = m["gaze"]
        fresh = now - self.ptr_still_since < 2.5
        if g == "scan":
            target = (math.sin(now * 1.6) * 0.8, 0.2)
        elif g is not None and not fresh:
            target = g
        if now < self.glance_until:
            target = self.glance
        elif random.random() < 0.003 and not fresh:
            self.glance = (random.uniform(-1, 1), random.uniform(-0.6, 0.6))
            self.glance_until = now + random.uniform(0.6, 1.4)
        self.gaze[0] += (target[0] - self.gaze[0]) * 0.3
        self.gaze[1] += (target[1] - self.gaze[1]) * 0.3

    def _blink(self, now: float) -> float:
        if self.blink_t < 0 and now >= self.blink_at:
            self.blink_t = now
        if self.blink_t >= 0:
            p = (now - self.blink_t) / 0.16
            if p >= 1:
                self.blink_t = -1
                self.blink_at = now + random.uniform(2.5, 6) * (2 if self.mood == "sleepy" else 1)
                return 1.0
            return abs(1 - 2 * p)
        return 1.0


PID_FILE = ROSE_HOME / "companion.pid"
_REVERSE = bytes(int(f"{b:08b}"[::-1], 2) for b in range(256))  # MSB-first PIL bits -> LSB-first X bitmaps


def _running_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def stop() -> bool:
    """Close Rose's face if it's showing. True if one was running."""
    pid = _running_pid()
    if pid is None:
        return False
    import signal

    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if _running_pid() is None:
            break
        time.sleep(0.1)
    return True


def set_wake(on: bool) -> int:
    """`rose face --wake on|off`: remember the choice and restart a running face so it takes effect."""
    _save_state(wake=on)
    print(f'"Hey Rose" wake word {"on" if on else "off"}.')
    if _running_pid() is not None:
        stop()
        start_detached()
    return 0


WHEEL_PID_FILE = ROSE_HOME / "wheel.pid"


def source_dir() -> Path | None:
    """The ROSE source folder a `uv tool install .` copy was built from (PEP 610 direct_url.json), else None.
    The copy in ~/.local/bin has no desktop/ folder next to it; the source folder does."""
    try:
        from importlib.metadata import distribution
        from urllib.parse import unquote, urlparse

        info = json.loads(distribution("rose").read_text("direct_url.json") or "{}")
        u = urlparse(info.get("url", ""))
        return Path(unquote(u.path)) if u.scheme == "file" and "dir_info" in info else None
    except Exception:  # noqa: BLE001 - not installed from a folder
        return None


def wheel_launcher() -> Path | None:
    """desktop/rose-wheel (ROSE_WHEEL_CMD overrides), if it and its Electron are there: next to this package
    (repo checkout / editable install), else in the folder a uv tool copy was installed from."""
    env = os.environ.get("ROSE_WHEEL_CMD")
    roots = [Path(__file__).resolve().parents[1]]
    if (src := source_dir()) is not None:
        roots.append(src)
    for p in [Path(env).expanduser()] if env else [r / "desktop" / "rose-wheel" for r in roots]:
        if p.is_file() and (p.parent / "node_modules" / "electron").is_dir():
            return p
    return None


def _wheel_pid() -> int | None:
    try:
        pid = int(WHEEL_PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def wheel_button_shown() -> bool:
    """Whether the wheel's floating round button shows (state `wheel_button`). Hidden: Super+C still opens it."""
    return bool(_state().get("wheel_button", True))


def _wheel_send(*args: str) -> bool:
    """Pass args to the running wheel: a second `rose-wheel` hands them over (single instance) and exits."""
    cmd = wheel_launcher()
    if cmd is None or _wheel_pid() is None:
        return False
    env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
    try:
        subprocess.Popen([str(cmd), *args], env=env, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        _log(f"menu wheel: {e}")
        return False
    return True


def set_wheel_button(on: bool) -> None:
    """Show/hide the wheel's floating button (remembered; a running wheel follows at once)."""
    _save_state(wheel_button=on)
    _wheel_send("--button=" + ("show" if on else "hide"))


def trigger_wheel() -> int:
    """`rose face --wheel` (Super+C): open/close the menu wheel; starts it if it isn't running."""
    if _wheel_pid() is not None:
        return 0 if _wheel_send("--toggle") else 1
    return 0 if start_wheel(open_now=True) else 1


def start_wheel(open_now: bool = False) -> bool:
    """Start the menu wheel (its own process, talks to the same rose-phone backend). True if it runs now."""
    if _wheel_pid() is not None:
        return True
    cmd = wheel_launcher()
    if cmd is None:
        _log("menu wheel: desktop/rose-wheel or its Electron (desktop/node_modules) not found")
        return False
    base, _ = _backend()
    env = {**os.environ, "ROSE_WHEEL_URL": base, "ROSE_WHEEL_PID": str(WHEEL_PID_FILE),
           "ROSE_WHEEL_ENV": str(load_config().get("companion_env_file", "~/0x/projects/rose-phone-app/backend/.env")),
           "ROSE_WHEEL_BUTTON": "show" if wheel_button_shown() else "hide"}
    env.pop("ELECTRON_RUN_AS_NODE", None)
    try:
        subprocess.Popen([str(cmd), *(["--open"] if open_now else [])], env=env, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        _log(f"menu wheel: {e}")
        return False
    return True


def stop_wheel() -> None:
    pid = _wheel_pid()
    if pid is not None:
        import signal

        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def start_detached() -> None:
    """Start `rose face` in its own session (survives the caller)."""
    subprocess.Popen(["rose", "face"], start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run(action: str = "show") -> int:
    """`rose face` [--stop | --restart]: one companion at a time."""
    ROSE_HOME.mkdir(parents=True, exist_ok=True)
    if action == "stop":
        print("Rose stopped listening." if stop() else "Rose wasn't running.")
        return 0
    if action == "restart":
        stop()
        time.sleep(0.3)
    lock = open(ROSE_HOME / ".companion.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # already running, maybe closed to the background: bring her back instead of doing nothing
        if trigger_show() == 0:
            print("Rose is back on screen (she was listening in the background).")
        else:
            print("Rose's face is already on screen (rose face --restart to reload it).")
        return 0
    if not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"] = ":0"
    _save_state(hidden=False)  # a fresh start always shows her
    PID_FILE.write_text(str(os.getpid()))
    import signal

    from rose import face_electron

    if face_electron.wanted():  # the Electron face (desktop/face); `rose face --ui tk` = this classic one
        try:
            return face_electron.run()
        finally:
            if _running_pid() == os.getpid():
                PID_FILE.unlink(missing_ok=True)
    app = Companion()
    signal.signal(signal.SIGTERM, lambda *a: app.after(0, app.destroy))
    signal.signal(signal.SIGUSR1, lambda *a: app.events.put(("hotkey", None)))  # `rose face --talk`
    signal.signal(signal.SIGUSR2, lambda *a: app.after(0, app.apply_visibility))  # `rose face` / `--hide`
    app.after(500, lambda: None)  # keeps the Tcl loop waking so the signal handler runs
    try:
        app.mainloop()
    finally:
        if _running_pid() == os.getpid():
            PID_FILE.unlink(missing_ok=True)
    return 0
