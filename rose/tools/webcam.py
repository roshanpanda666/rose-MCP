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
# One ffmpeg (transient user service rose-webcam-live) keeps overwriting a single JPEG, ~2 fps, so the phone's
# live view can poll it cheaply. Nothing is kept: the file is replaced every frame and deleted on stop. The
# stream ends by itself LIVE_SECONDS after it was last asked for a frame (the camera light shows while it runs).

LIVE_UNIT = "rose-webcam-live"
LIVE_FILE = WEBCAM_DIR / ".live.jpg"
LIVE_SECONDS = 60


def _live_active() -> bool:
    from rose.common import systemctl_user

    return systemctl_user("is-active", LIVE_UNIT) == "active"


def webcam_live_frame(device: str = "") -> list:
    """Latest frame of the webcam live view (starts the camera stream; stops 60 s after the last request)."""
    import json
    import time

    from mcp.server.mcpserver import Image

    require("ffmpeg", "sudo apt install ffmpeg")
    cams = {c["device"]: c["name"] for c in list_cameras()}
    if not cams:
        raise ToolError("No webcam found.")
    device = device or load_config().get("webcam_device") or next(iter(cams))
    if device not in cams:
        raise ToolError(f"{device} is not a capture device. Cameras: {cams}")
    WEBCAM_DIR.mkdir(parents=True, exist_ok=True)
    WEBCAM_DIR.chmod(0o700)
    stamp = WEBCAM_DIR / ".live-last-request"
    stamp.touch()
    if not _live_active():
        LIVE_FILE.unlink(missing_ok=True)
        # The service stops itself: ffmpeg runs at most LIVE_SECONDS; the client keeps it alive by asking again.
        run(["systemd-run", "--user", "--collect", f"--unit={LIVE_UNIT}", "--property=KillSignal=SIGINT",
             "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2", "-video_size", "640x480", "-i", device,
             "-t", str(LIVE_SECONDS), "-vf", "fps=2", "-q:v", "6", "-update", "1", "-y", str(LIVE_FILE)])
        notify_desktop("ROSE webcam live view started", f"{cams[device]} (stops by itself when nobody watches)",
                       urgency="critical")
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if LIVE_FILE.exists() and time.time() - LIVE_FILE.stat().st_mtime < 3 and LIVE_FILE.stat().st_size > 0:
            break
        time.sleep(0.15)
    else:
        raise ToolError("The webcam didn't send a picture (is another app using it?).")
    data = LIVE_FILE.read_bytes()
    return [json.dumps({"camera": cams[device], "age_s": round(time.time() - LIVE_FILE.stat().st_mtime, 2)}),
            Image(data=data, format="jpeg")]


def webcam_live_stop() -> str:
    """Stop the webcam live view and delete its frame."""
    from rose.common import systemctl_user

    was = _live_active()
    if was:
        systemctl_user("stop", LIVE_UNIT)
    LIVE_FILE.unlink(missing_ok=True)
    if was:
        notify_desktop("ROSE webcam live view stopped", "", urgency="low")
    return "Webcam live view stopped." if was else "The webcam live view wasn't running."
