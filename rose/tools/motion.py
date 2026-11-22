"""Motion watch: the webcam watches the room and logs every frame that changes.

One ffmpeg (transient user service rose-motion) reads the camera at 2 fps and:
  - keeps overwriting the webcam live-view frame (so the live view still works while watching), and
  - runs ffmpeg's scene-change score on each frame; frames above the threshold overwrite motion.jpg and
    append a line to events.log.
Clients poll motion_events with the byte offset they got last time and receive only what's new.
Person detection runs in the client (the phone app), on the motion frames.

Privacy: starting is approval-gated for remote clients (camera), the laptop shows a critical notification,
the camera light stays on, the watch stops by itself after `hours`, and frames go only to the client that
started the watch (or local Claude Code). Nothing is kept after motion_watch_stop.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Literal

from rose.common import ToolError, notify_desktop, require, run, systemctl_user
from rose.config import ROSE_HOME, load_config

MOTION_DIR = ROSE_HOME / "motion"
UNIT = "rose-motion"
LOG = MOTION_DIR / "events.log"
FRAME = MOTION_DIR / "motion.jpg"
STATE = MOTION_DIR / "state.json"
# ffmpeg scene score (0-1) a frame must beat. Sitting still ~0.02-0.03 on a typical laptop camera.
THRESHOLDS = {"low": 0.08, "medium": 0.045, "high": 0.025}
WARMUP_S = 3  # the first frames jump while auto-exposure settles
MAX_IMAGE_BYTES = 400_000


def _active() -> bool:
    return systemctl_user("is-active", UNIT) == "active"


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _caller() -> str:
    from rose import guard

    return guard.effective_caller()


def motion_watch_start(sensitivity: Literal["low", "medium", "high"] = "medium", hours: float = 8,
                       device: str = "") -> dict:
    """Watch the room with the webcam and log motion (stops by itself after `hours`, max 24)."""
    from rose.tools import webcam

    if _active():
        st = _state()
        return {"status": "already_watching", "sensitivity": st.get("sensitivity"), "until": st.get("until")}
    require("ffmpeg", "sudo apt install ffmpeg")
    if sensitivity not in THRESHOLDS:
        raise ToolError("sensitivity must be low, medium or high.")
    hours = min(24.0, max(0.1, float(hours)))
    cams = {c["device"]: c["name"] for c in webcam.list_cameras()}
    if not cams:
        raise ToolError("No webcam found.")
    device = device or load_config().get("webcam_device") or next(iter(cams))
    if device not in cams:
        raise ToolError(f"{device} is not a capture device. Cameras: {cams}")
    if webcam._live_active():  # one camera user at a time: the watch takes over the live view's frame file
        systemctl_user("stop", webcam.LIVE_UNIT)
    MOTION_DIR.mkdir(parents=True, exist_ok=True)
    MOTION_DIR.chmod(0o700)
    webcam.WEBCAM_DIR.mkdir(parents=True, exist_ok=True)
    webcam.WEBCAM_DIR.chmod(0o700)
    for f in (LOG, FRAME):
        f.unlink(missing_ok=True)
    LOG.touch(mode=0o600)
    thr = THRESHOLDS[sensitivity]
    graph = (f"[0:v]fps=2,split=2[a][b];[b]select='gt(scene\\,{thr})*gte(t\\,{WARMUP_S})',"
             f"metadata=print:file={LOG}:direct=1[m]")
    until = time.time() + hours * 3600
    run(["systemd-run", "--user", "--collect", f"--unit={UNIT}", "--property=KillSignal=SIGINT",
         f"--property=RuntimeMaxSec={int(hours * 3600)}",
         "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2", "-video_size", "640x480", "-i", device,
         "-filter_complex", graph,
         "-map", "[a]", "-q:v", "6", "-update", "1", "-atomic_writing", "1", "-y", str(webcam.LIVE_FILE),
         "-map", "[m]", "-q:v", "4", "-fps_mode", "vfr", "-update", "1", "-atomic_writing", "1", "-y", str(FRAME)])
    STATE.write_text(json.dumps({"owner": _caller(), "sensitivity": sensitivity, "device": device,
                                 "started": time.time(), "until": until}))
    STATE.chmod(0o600)
    notify_desktop("ROSE motion watch is ON", f"{cams[device]} watches the room until "
                   f"{datetime.fromtimestamp(until):%H:%M} (camera light stays on)", urgency="critical")
    return {"status": "watching", "sensitivity": sensitivity, "until": until, "camera": cams[device]}


def motion_events(since: int = 0) -> list | dict:
    """New motion since byte offset `since` (pass back the returned offset); latest motion frame if any."""
    from rose import guard

    from mcp.server.mcpserver import Image

    watching = _active()
    st = _state()
    out: dict = {"watching": watching, "until": st.get("until"), "sensitivity": st.get("sensitivity")}
    try:
        size = LOG.stat().st_size
    except OSError:
        return {**out, "offset": 0, "new": 0}
    since = max(0, int(since))
    if since > size:  # a new watch started: the log was replaced
        since = 0
    with LOG.open("rb") as f:
        f.seek(since)
        chunk = f.read(size - since)
    end = chunk.rfind(b"\n") + 1  # only whole lines; the rest comes next time
    lines = chunk[:end].decode(errors="replace").splitlines()
    scores = [float(x.split("=", 1)[1]) for x in lines if x.startswith("lavfi.scene_score=")]
    out.update(offset=since + end, new=len(scores), max_score=round(max(scores), 3) if scores else None)
    if not scores or not FRAME.exists():
        return out
    who = guard.effective_caller()
    if guard.is_remote(who) and who != st.get("owner"):
        out["image"] = "withheld: frames go only to the client that started the watch"
        return out
    data = FRAME.read_bytes()
    if not data or len(data) > MAX_IMAGE_BYTES:
        return out
    out["frame_age_s"] = round(time.time() - FRAME.stat().st_mtime, 2)
    return [json.dumps(out), Image(data=data, format="jpeg")]


def motion_watch_stop() -> str:
    """Stop the motion watch and delete its frames and log."""
    from rose.tools import webcam

    was = _active()
    if was:
        systemctl_user("stop", UNIT)
    for f in (LOG, FRAME, STATE, webcam.LIVE_FILE):
        f.unlink(missing_ok=True)
    if was:
        notify_desktop("ROSE motion watch stopped", "", urgency="low")
    return "Motion watch stopped." if was else "The motion watch wasn't running."
