"""Meeting recorder: record the laptop microphone (optionally plus system audio) to ~/rose/recordings.

Starting a recording needs confirm=true (remote clients get a passphrase approval link) and always shows a
desktop notification. ffmpeg runs as a transient systemd user service (rose-recording), so it survives
ROSE restarts and is stopped cleanly with SIGINT, which finalizes the file. Recordings stop by themselves
after 3 hours. Transcribing and summarizing happen in the client (e.g. the phone app, with Groq Whisper).
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import time
from datetime import datetime

from rose.common import ToolError, notify_desktop, run, run_text, systemctl_user
from rose.config import ROSE_HOME

REC_DIR = ROSE_HOME / "recordings"
STATE = REC_DIR / ".current.json"
UNIT = "rose-recording"
MAX_SECONDS = 3 * 3600


def _active() -> bool:
    return systemctl_user("is-active", UNIT) == "active"


def _default_sink_monitor() -> str | None:
    try:
        sink = run_text(["pactl", "get-default-sink"])
    except ToolError:
        return None
    return f"{sink}.monitor" if sink else None


def recording_start(name: str = "", system_audio: bool = False, confirm: bool = False) -> str:
    """Start recording the mic (system_audio=true adds what the laptop plays). Needs confirm=true."""
    if not shutil.which("ffmpeg"):
        raise ToolError("ffmpeg is not installed (sudo apt install ffmpeg).")
    if _active():
        raise ToolError("A recording is already running. Stop it first (recording_stop).")
    if not confirm:
        raise ToolError("Recording the microphone is sensitive. Ask the user (and anyone in the meeting), then "
                        "call again with confirm=true.")
    REC_DIR.mkdir(parents=True, exist_ok=True)
    REC_DIR.chmod(0o700)
    slug = re.sub(r"[^\w-]+", "-", name.strip().lower()).strip("-")[:40]
    path = REC_DIR / f"{datetime.now():%Y%m%d-%H%M%S}{'-' + slug if slug else ''}.ogg"
    inputs = ["-f", "pulse", "-i", "default"]
    filt: list[str] = []
    if system_audio:
        monitor = _default_sink_monitor()
        if not monitor:
            raise ToolError("Couldn't find the speaker output to record system audio.")
        inputs += ["-f", "pulse", "-i", monitor]
        filt = ["-filter_complex", "amix=inputs=2:duration=longest"]
    run(["systemd-run", "--user", "--collect", f"--unit={UNIT}", "--property=KillSignal=SIGINT",
         "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs, *filt, "-t", str(MAX_SECONDS),
         "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k", str(path)])
    STATE.write_text(json.dumps({"path": str(path), "started": time.time(), "system_audio": system_audio}))
    notify_desktop("ROSE is recording", f"{path.name}" + (" (mic + system audio)" if system_audio else " (mic)"),
                   urgency="critical")
    return f"Recording started: {path.name}. Stop with recording_stop."


def recording_stop() -> dict:
    """Stop the current recording and return its file and length."""
    if not _active():
        raise ToolError("No recording is running.")
    run(["systemctl", "--user", "stop", UNIT], timeout=20)
    info = json.loads(STATE.read_text()) if STATE.exists() else {}
    STATE.unlink(missing_ok=True)
    minutes = round((time.time() - info.get("started", time.time())) / 60, 1)
    notify_desktop("ROSE stopped recording", f"{minutes} min")
    return {"file": info.get("path"), "minutes": minutes}


def recording_status() -> dict:
    """Whether a recording is running, and for how long."""
    if not _active():
        return {"recording": False}
    info = json.loads(STATE.read_text()) if STATE.exists() else {}
    return {"recording": True, "file": info.get("path"),
            "minutes": round((time.time() - info.get("started", time.time())) / 60, 1)}


def recording_list(limit: int = 10) -> list[dict]:
    """Recent recordings (name, size, date)."""
    if not REC_DIR.exists():
        return []
    files = sorted(REC_DIR.glob("*.ogg"), key=lambda p: p.stat().st_mtime, reverse=True)[:max(1, min(limit, 50))]
    return [{"name": p.name, "mb": round(p.stat().st_size / 1024**2, 1),
             "date": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="minutes")} for p in files]


# ---------------- live listening (phone app) ----------------
# ffmpeg (transient user service rose-mic-live) encodes the chosen microphone to Opus/WebM and serves it to ONE
# client on 127.0.0.1 only; the phone app's backend relays it over Tailscale. Nothing is written to disk. The
# stream ends when the listener disconnects, on mic_live_stop, or after LISTEN_MAX_SECONDS. Starting it always
# needs approval from remote clients (guard.ALWAYS_APPROVE) and shows a desktop notification.

LISTEN_UNIT = "rose-mic-live"
LISTEN_PORT = 8796
LISTEN_MAX_SECONDS = 3600


def _is_monitor(src: dict) -> bool:
    """Speaker "monitor" sources (what the laptop plays), not microphones."""
    return (src.get("properties") or {}).get("device.class") == "monitor" or src.get("name", "").endswith(".monitor")


def _sources() -> list[dict]:
    try:
        data = json.loads(run_text(["pactl", "-f", "json", "list", "sources"]))
    except (ToolError, ValueError):
        return []
    try:
        default = run_text(["pactl", "get-default-source"])
    except ToolError:
        default = ""
    return [{"name": s["name"], "description": s.get("description") or s["name"], "default": s["name"] == default}
            for s in data if not _is_monitor(s) and not s["name"].startswith(EC_PREFIX)]  # real microphones only


def _listening(port: int) -> bool:
    """Is something listening on 127.0.0.1:port? (reads /proc/net/tcp; connecting would use up the one client)"""
    want = f"0100007F:{port:04X}"
    try:
        lines = Path("/proc/net/tcp").read_text().splitlines()[1:]
    except OSError:
        return False
    return any(parts[1] == want and parts[3] == "0A" for parts in (ln.split() for ln in lines) if len(parts) > 3)


def mic_sources() -> list[dict]:
    """Microphones on the laptop (name, description, which is the default)."""
    mics = _sources()
    if not mics:
        raise ToolError("No microphones found (is PipeWire/PulseAudio running?).")
    return mics


# Echo cancelling while listening: PipeWire's echo-cancel module, loaded only for the listening session.
# Sound played into EC_SINK (the phone app's talk-live voice and spoken text) is removed from the mic, so the
# phone doesn't hear itself back. Other laptop audio (music, calls) still reaches the mic as usual. The default
# speaker and mic don't change; the module is unloaded when listening stops (ExecStopPost) or on the next start.
EC_PREFIX = "rose_ec"
EC_SOURCE, EC_SINK = "rose_ec_source", "rose_ec_sink"


def _ec_unload() -> None:
    try:
        mods = run_text(["pactl", "list", "short", "modules"])
    except ToolError:
        return
    for line in mods.splitlines():
        parts = line.split("\t")
        if len(parts) > 2 and "module-echo-cancel" in parts[1] and f"source_name={EC_SOURCE}" in parts[2]:
            run(["pactl", "unload-module", parts[0]], timeout=10)


def _ec_load(mic: str) -> str | None:
    """Load the echo canceller on `mic`; its module id, or None if it isn't available."""
    _ec_unload()
    try:
        sink = run_text(["pactl", "get-default-sink"]).strip()
        mod = run_text(["pactl", "load-module", "module-echo-cancel", "aec_method=webrtc",
                        f"source_name={EC_SOURCE}", f"sink_name={EC_SINK}", f"source_master={mic}",
                        f"sink_master={sink}"], timeout=10).strip()
    except ToolError:
        return None
    return mod if mod.isdigit() else None


def mic_live_start(source: str = "", echo_cancel: bool = True) -> dict:
    """Start live listening to a laptop mic (name from mic_sources; empty = default). Laptop is notified."""
    if not shutil.which("ffmpeg"):
        raise ToolError("ffmpeg is not installed (sudo apt install ffmpeg).")
    mics = _sources()
    mic = next((m for m in mics if m["name"] == source), None) if source else \
        next((m for m in mics if m["default"]), mics[0] if mics else None)
    if mic is None:
        raise ToolError(f"No microphone named {source!r}. Use mic_sources.")
    if systemctl_user("is-active", LISTEN_UNIT) == "active":
        run(["systemctl", "--user", "stop", LISTEN_UNIT], timeout=10)
    capture, cleanup = mic["name"], []
    mod = _ec_load(mic["name"]) if echo_cancel and shutil.which("pactl") else None
    if mod:
        capture = EC_SOURCE
        cleanup = [f"--property=ExecStopPost={shutil.which('pactl')} unload-module {mod}"]
    run(["systemd-run", "--user", "--collect", f"--unit={LISTEN_UNIT}", f"--property=RuntimeMaxSec={LISTEN_MAX_SECONDS}",
         *cleanup,
         "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "pulse", "-i", capture, "-ac", "1", "-ar", "48000",
         "-c:a", "libopus", "-b:a", "48k", "-application", "voip", "-f", "webm", "-live", "1",
         "-cluster_time_limit", "500", "-listen", "1", f"http://127.0.0.1:{LISTEN_PORT}/live.webm"])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:  # wait until ffmpeg is listening (never connect: it serves one client)
        if _listening(LISTEN_PORT):
            break
        time.sleep(0.1)
    else:
        run(["systemctl", "--user", "stop", LISTEN_UNIT], timeout=10)
        _ec_unload()
        raise ToolError("The microphone stream didn't start (is the mic in use or muted?).")
    notify_desktop("ROSE: someone is listening through the microphone", mic["description"], urgency="critical")
    return {"stream": f"http://127.0.0.1:{LISTEN_PORT}/live.webm", "source": mic["name"],
            "description": mic["description"], "max_minutes": LISTEN_MAX_SECONDS // 60,
            "echo_cancel": bool(mod), "play_to": EC_SINK if mod else ""}


def mic_live_stop() -> str:
    """Stop live listening."""
    if systemctl_user("is-active", LISTEN_UNIT) != "active":
        _ec_unload()
        return "Live listening wasn't running."
    run(["systemctl", "--user", "stop", LISTEN_UNIT], timeout=10)
    _ec_unload()
    notify_desktop("ROSE stopped listening", "", urgency="low")
    return "Stopped listening."
