"""Webcam capture via ffmpeg (V4L2). Every capture shows a desktop notification."""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from rose.common import ToolError, display_env, require, run
from rose.config import ROSE_HOME, load_config

WEBCAM_DIR = ROSE_HOME / "webcam"
_V4L = Path("/sys/class/video4linux")


def list_cameras() -> list[dict]:
    """List webcams that can capture images (one entry per camera, not per metadata node)."""
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
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "--app-name=ROSE", "--urgency=low", "ROSE used the webcam",
                        f"{cams[device]} -> {out.name}"], env=display_env(), capture_output=True)
    return str(out)
