"""Webcam capture via ffmpeg (V4L2). Every capture shows a desktop notification."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from rose.common import ToolError, notify_desktop, require, run
from rose.config import ROSE_HOME, load_config

WEBCAM_DIR = ROSE_HOME / "webcam"
_V4L = Path("/sys/class/video4linux")


def list_cameras() -> list[dict]:
    """Webcams that can capture."""
    cams = []
    if not _V4L.is_dir():
        return cams
    for d in sorted(_V4L.iterdir()):
        try:
            # index 0 is the capture node; other indexes are metadata streams of the same camera.
            if (d / "index").read_text().strip() != "0":
                continue
            name = (d / "name").read_text().strip()
        except OSError:
            continue
        cams.append({"device": f"/dev/{d.name}", "name": name})
    return cams


def webcam_capture(device: str = "") -> str:
    """Take one photo from a webcam and return the saved JPEG path. Default device from config."""
    require("ffmpeg", "sudo apt install ffmpeg")
    cams = {c["device"]: c["name"] for c in list_cameras()}
    if not cams:
        raise ToolError("No webcam found.")
    device = device or load_config().get("webcam_device") or next(iter(cams))
    if device not in cams:
        raise ToolError(f"{device} is not a capture device. Cameras: {cams}")
    WEBCAM_DIR.mkdir(parents=True, exist_ok=True)
    out = WEBCAM_DIR / f"webcam-{datetime.now():%Y%m%d-%H%M%S}.jpg"
    from rose.tools import motion

    if (motion._active() or _live_active()) and LIVE_FILE.exists():  # camera busy: save the live frame
        out.write_bytes(LIVE_FILE.read_bytes())
        notify_desktop("ROSE used the webcam", f"{cams[device]} -> {out.name}", urgency="low")
        return str(out)
    if _stream_active():
        raise ToolError("The phone's live webcam video is using the camera; close it (webcam_live_stop) first.")
    # Skip the first ~1s of frames so auto-exposure/white balance can settle.
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2"]
    tail = ["-i", device, "-vf", "select=gte(n\\,20)", "-frames:v", "1", "-q:v", "3", "-y", str(out)]
    try:
        run(base + ["-video_size", "1280x720"] + tail, timeout=20)
    except ToolError:
        run(base + tail, timeout=20)  # camera doesn't do 720p; use its default size
    if not out.exists() or out.stat().st_size == 0:
        raise ToolError("ffmpeg ran but no image was written (is another app using the camera?).")
    notify_desktop("ROSE used the webcam", f"{cams[device]} -> {out.name}", urgency="low")
    return str(out)


# ---------------- live view ----------------
# Two ways to watch the webcam from the phone app; both start a transient user service and stop by themselves.
#
# Poll (webcam_live_frame): one ffmpeg (rose-webcam-live) keeps overwriting a single JPEG (atomic writes, so a
#   read never sees half a frame). The client asks for the frame newer than the one it has (`after`). A small
#   shell loop around ffmpeg stops it LIVE_SECONDS after the last request; RuntimeMaxSec caps it.
# Stream (webcam_live_stream): one ffmpeg (rose-webcam-stream) serves multipart MJPEG to ONE client on
#   127.0.0.1 only; the phone app's backend relays it. It ends when the viewer disconnects, when nobody
#   connects within STREAM_ACCEPT_MS, on webcam_live_stop, or after STREAM_MAX_SECONDS.
# Nothing is kept: the poll frame is deleted on stop and the stream never touches disk. The camera light shows.

LIVE_UNIT = "rose-webcam-live"
LIVE_FILE = WEBCAM_DIR / ".live.jpg"
LIVE_STATE = WEBCAM_DIR / ".live-state"  # {"device", "quality"} of the running poll view (to switch cameras)
LIVE_STAMP = WEBCAM_DIR / ".live-last-request"
LIVE_SECONDS = 60          # poll view stops this long after the last frame request
LIVE_MAX_SECONDS = 1800    # hard cap per poll session

STREAM_UNIT = "rose-webcam-stream"
STREAM_PORT = 8797
STREAM_PATH = "/cam.mjpg"
STREAM_BOUNDARY = "ffmpeg"  # ffmpeg's mpjpeg default; the relay must send it in its Content-Type
STREAM_ACCEPT_MS = 20000
STREAM_MAX_SECONDS = 1800

# quality -> (size, poll fps, poll JPEG q, stream fps, stream JPEG q). Lower q = sharper, bigger.
QUALITIES = {
    "low": ("640x480", 4, 6, 10, 6),
    "medium": ("960x544", 6, 4, 15, 5),
    "high": ("1280x720", 8, 3, 20, 4),
}

# Runs ffmpeg ("$@") and stops it once $1 (the request stamp) is older than $2 seconds.
_IDLE_WRAPPER = (
    'stamp=$1; idle=$2; shift 2; "$@" & p=$!; '
    'trap \'kill -INT $p 2>/dev/null\' INT TERM; '
    'while kill -0 $p 2>/dev/null; do sleep 2; '
    'm=$(stat -c %Y "$stamp" 2>/dev/null || echo 0); '
    'if [ $(( $(date +%s) - m )) -gt "$idle" ]; then kill -INT $p 2>/dev/null; break; fi; done; '
    'wait $p'
)

_mjpeg_cache: dict[str, bool] = {}


def _quality(quality: str = "") -> str:
    q = (quality or load_config().get("webcam_live_quality") or "medium").lower()
    if q not in QUALITIES:
        raise ToolError(f"quality must be one of {', '.join(QUALITIES)}")
    return q


def _supports_mjpeg(device: str) -> bool:
    """Does the camera offer Motion-JPEG? (raw YUYV is bandwidth-capped to ~10 fps at 720p on USB 2)"""
    if device not in _mjpeg_cache:
        import subprocess

        try:
            p = subprocess.run(["ffmpeg", "-hide_banner", "-f", "v4l2", "-list_formats", "compressed", "-i", device],
                               capture_output=True, text=True, timeout=10)
            _mjpeg_cache[device] = "mjpeg" in p.stderr.lower()
        except (OSError, subprocess.TimeoutExpired):
            _mjpeg_cache[device] = False
    return _mjpeg_cache[device]


def _input_args(device: str, size: str, low_delay: bool = True) -> list[str]:
    """ffmpeg input args. low_delay=False for multi-output graphs (with nobuffer they never start writing)."""
    fmt = ["-input_format", "mjpeg", "-framerate", "30"] if _supports_mjpeg(device) else []
    fast = ["-fflags", "nobuffer", "-flags", "low_delay"] if low_delay else []
    return ["-f", "v4l2", *fmt, "-video_size", size, *fast, "-i", device]


def _active(unit: str) -> bool:
    from rose.common import systemctl_user

    return systemctl_user("is-active", unit) == "active"


def _live_active() -> bool:
    return _active(LIVE_UNIT)


def _stream_active() -> bool:
    return _active(STREAM_UNIT)


def _stop_unit(unit: str) -> None:
    import time

    from rose.common import systemctl_user

    systemctl_user("stop", unit)
    for _ in range(20):  # the camera is free only once ffmpeg has exited
        if not _active(unit):
            return
        time.sleep(0.1)


def _pick_camera(device: str) -> tuple[str, dict]:
    require("ffmpeg", "sudo apt install ffmpeg")
    cams = {c["device"]: c["name"] for c in list_cameras()}
    if not cams:
        raise ToolError("No webcam found.")
    device = device or load_config().get("webcam_device") or next(iter(cams))
    if device not in cams:
        raise ToolError(f"{device} is not a capture device. Cameras: {cams}")
    WEBCAM_DIR.mkdir(parents=True, exist_ok=True)
    WEBCAM_DIR.chmod(0o700)
    return device, cams


def _poll_cmd(device: str, quality: str) -> list[str]:
    size, fps, q, _, _ = QUALITIES[quality]
    ffmpeg = ["ffmpeg", "-hide_banner", "-loglevel", "error", *_input_args(device, size),
              "-vf", f"fps={fps}", "-q:v", str(q), "-update", "1", "-atomic_writing", "1", "-y", str(LIVE_FILE)]
    return ["systemd-run", "--user", "--collect", f"--unit={LIVE_UNIT}", "--property=KillSignal=SIGINT",
            f"--property=RuntimeMaxSec={LIVE_MAX_SECONDS}",
            "sh", "-c", _IDLE_WRAPPER, "rose-webcam-live", str(LIVE_STAMP), str(LIVE_SECONDS), *ffmpeg]


def webcam_live_frame(device: str = "", after: float = 0, quality: str = "") -> list:
    """Latest webcam live-view frame (starts the camera; stops 60 s after the last request). after: mtime of
    the frame you have (waits ~1.5 s for a newer one, else "same"). quality: low/medium/high."""
    import json
    import time

    from mcp.server.mcpserver import Image

    device, cams = _pick_camera(device)
    quality = _quality(quality)
    LIVE_STAMP.touch()
    from rose.tools import motion

    if not motion._active():  # (the motion watch, when on, already streams the camera into LIVE_FILE)
        if _stream_active():
            _stop_unit(STREAM_UNIT)  # one camera, one reader: the poll view takes over
        want = {"device": device, "quality": quality}
        if _live_active():
            try:
                running = json.loads(LIVE_STATE.read_text())
            except (OSError, ValueError):
                running = {}
            if running != want:
                _stop_unit(LIVE_UNIT)  # switching camera or quality: restart on the new settings
        if not _live_active():
            LIVE_FILE.unlink(missing_ok=True)
            LIVE_STATE.write_text(json.dumps(want))
            run(_poll_cmd(device, quality))
            notify_desktop("ROSE webcam live view started", f"{cams[device]} (stops by itself when nobody watches)",
                           urgency="critical")
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if LIVE_FILE.exists() and time.time() - LIVE_FILE.stat().st_mtime < 3 and LIVE_FILE.stat().st_size > 0:
            break
        time.sleep(0.05)
    else:
        raise ToolError("The webcam didn't send a picture (is another app using it?).")
    if after:  # long-poll: wait for a frame newer than the client's
        newer_by = time.monotonic() + 1.5
        while LIVE_FILE.stat().st_mtime <= after and time.monotonic() < newer_by:
            time.sleep(0.03)
    mtime = LIVE_FILE.stat().st_mtime
    shown = device
    if motion._active():  # the watch's camera is what's on screen; it can't switch until the watch stops
        try:
            shown = json.loads(motion.STATE.read_text()).get("device") or device
        except (OSError, ValueError):
            pass
    meta = {"camera": cams.get(shown, cams[device]), "device": shown, "quality": quality,
            "mtime": mtime, "age_s": round(time.time() - mtime, 2)}
    if after and mtime <= after:
        return [json.dumps({**meta, "same": True})]
    return [json.dumps(meta), Image(data=LIVE_FILE.read_bytes(), format="jpeg")]


def webcam_live_stream(device: str = "", quality: str = "") -> dict:
    """Start a smooth live webcam video (MJPEG over HTTP on 127.0.0.1, ONE viewer) for the phone app to relay.
    Ends when the viewer leaves. Use webcam_live_frame instead while the motion watch holds the camera."""
    import time

    from rose.tools import motion
    from rose.tools.recorder import _listening

    device, cams = _pick_camera(device)
    quality = _quality(quality)
    if motion._active():
        raise ToolError("The motion watch holds the camera; use webcam_live_frame (or motion_watch_stop).")
    for unit in (LIVE_UNIT, STREAM_UNIT):
        if _active(unit):
            _stop_unit(unit)
    LIVE_FILE.unlink(missing_ok=True)
    size, _, _, fps, q = QUALITIES[quality]
    run(["systemd-run", "--user", "--collect", f"--unit={STREAM_UNIT}", "--property=KillSignal=SIGINT",
         f"--property=RuntimeMaxSec={STREAM_MAX_SECONDS}",
         "ffmpeg", "-hide_banner", "-loglevel", "error", *_input_args(device, size),
         "-vf", f"fps={fps}", "-q:v", str(q), "-f", "mpjpeg", "-boundary_tag", STREAM_BOUNDARY,
         "-listen_timeout", str(STREAM_ACCEPT_MS), "-listen", "1", f"http://127.0.0.1:{STREAM_PORT}{STREAM_PATH}"])
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:  # wait until ffmpeg is listening (never connect: it serves one client)
        if _listening(STREAM_PORT):
            break
        if not _stream_active():
            break  # ffmpeg already exited (camera busy?)
        time.sleep(0.05)
    else:
        _stop_unit(STREAM_UNIT)
        raise ToolError("The webcam stream didn't start (is another app using the camera?).")
    if not _listening(STREAM_PORT):
        raise ToolError("The webcam stream didn't start (is another app using the camera?).")
    notify_desktop("ROSE webcam live video started", f"{cams[device]} (ends when the viewer leaves)",
                   urgency="critical")
    return {"stream": f"http://127.0.0.1:{STREAM_PORT}{STREAM_PATH}",
            "content_type": f"multipart/x-mixed-replace;boundary={STREAM_BOUNDARY}",
            "camera": cams[device], "device": device, "quality": quality, "size": size, "fps": fps,
            "max_minutes": STREAM_MAX_SECONDS // 60}


def webcam_live_stop() -> str:
    """Stop the webcam live view / live video and delete its frame."""
    from rose.tools import motion

    if motion._active():
        return "Live view closed. The motion watch keeps the camera on (motion_watch_stop turns it off)."
    was = [u for u in (LIVE_UNIT, STREAM_UNIT) if _active(u)]
    for unit in was:
        _stop_unit(unit)
    LIVE_FILE.unlink(missing_ok=True)
    if was:
        notify_desktop("ROSE webcam live view stopped", "", urgency="low")
    return "Webcam live view stopped." if was else "The webcam live view wasn't running."
