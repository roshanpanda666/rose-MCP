"""Webcam live view: poll command, long-poll `after`, MJPEG stream start/stop (no real camera or systemd)."""

import json
import os
import time

import pytest

from rose.common import ToolError
from rose.tools import motion, webcam

CAM = [{"device": "/dev/video0", "name": "Test Cam"}]


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """Fake camera + systemd: `units` holds the active unit names, `cmds` every systemd-run command."""
    state = {"units": set(), "cmds": [], "stopped": []}
    monkeypatch.setattr(webcam, "WEBCAM_DIR", tmp_path)
    monkeypatch.setattr(webcam, "LIVE_FILE", tmp_path / ".live.jpg")
    monkeypatch.setattr(webcam, "LIVE_STATE", tmp_path / ".live-state")
    monkeypatch.setattr(webcam, "LIVE_STAMP", tmp_path / ".live-last-request")
    monkeypatch.setattr(webcam, "list_cameras", lambda: CAM)
    monkeypatch.setattr(webcam, "require", lambda *a: "ffmpeg")
    monkeypatch.setattr(webcam, "_supports_mjpeg", lambda d: True)
    monkeypatch.setattr(webcam, "_active", lambda unit: unit in state["units"])
    monkeypatch.setattr(motion, "_active", lambda: False)

    def stop(unit):
        state["stopped"].append(unit)
        state["units"].discard(unit)

    monkeypatch.setattr(webcam, "_stop_unit", stop)

    def run(cmd, **k):
        state["cmds"].append(cmd)
        unit = next(a.split("=", 1)[1] for a in cmd if a.startswith("--unit="))
        state["units"].add(unit)
        if unit == webcam.LIVE_UNIT:
            webcam.LIVE_FILE.write_bytes(b"\xff\xd8jpeg")
        return b""

    monkeypatch.setattr(webcam, "run", run)
    return state


def test_poll_uses_mjpeg_atomic_writes_and_idle_watchdog(fake):
    out = webcam.webcam_live_frame(quality="high")
    meta = json.loads(out[0])
    assert meta["quality"] == "high" and meta["device"] == "/dev/video0" and len(out) == 2
    cmd = fake["cmds"][0]
    assert "-t" not in cmd  # no hard 60 s cut-off any more: the idle loop stops it
    assert cmd[cmd.index("-input_format") + 1] == "mjpeg"
    assert cmd[cmd.index("-video_size") + 1] == "1280x720"
    assert "-atomic_writing" in cmd and "fps=8" in cmd
    assert f"--property=RuntimeMaxSec={webcam.LIVE_MAX_SECONDS}" in cmd
    assert cmd[cmd.index("-c") + 3:cmd.index("-c") + 5] == [str(webcam.LIVE_STAMP), str(webcam.LIVE_SECONDS)]


def test_poll_restarts_only_on_new_settings(fake):
    webcam.webcam_live_frame(quality="low")
    webcam.webcam_live_frame(quality="low")
    assert len(fake["cmds"]) == 1 and not fake["stopped"]
    webcam.webcam_live_frame(quality="medium")
    assert fake["stopped"] == [webcam.LIVE_UNIT] and len(fake["cmds"]) == 2


def test_after_returns_same_without_image_when_no_newer_frame(fake, monkeypatch):
    first = json.loads(webcam.webcam_live_frame()[0])
    monkeypatch.setattr(time, "sleep", lambda s: None)
    out = webcam.webcam_live_frame(after=first["mtime"])
    assert len(out) == 1 and json.loads(out[0])["same"] is True
    later = first["mtime"] + 0.2
    os.utime(webcam.LIVE_FILE, (later, later))
    out = webcam.webcam_live_frame(after=first["mtime"])
    assert len(out) == 2 and json.loads(out[0])["mtime"] == pytest.approx(later)


def test_bad_quality_rejected(fake):
    with pytest.raises(ToolError, match="quality"):
        webcam.webcam_live_frame(quality="ultra")


def test_stream_takes_camera_from_poll_and_serves_localhost(fake, monkeypatch):
    import rose.tools.recorder as recorder

    monkeypatch.setattr(recorder, "_listening", lambda port: port == webcam.STREAM_PORT)
    webcam.webcam_live_frame()
    info = webcam.webcam_live_stream(quality="medium")
    assert webcam.LIVE_UNIT in fake["stopped"] and not webcam.LIVE_FILE.exists()
    assert info["stream"] == f"http://127.0.0.1:{webcam.STREAM_PORT}/cam.mjpg"
    assert info["content_type"] == "multipart/x-mixed-replace;boundary=ffmpeg" and info["fps"] == 15
    cmd = fake["cmds"][-1]
    assert cmd[cmd.index("-listen") + 1] == "1" and "-listen_timeout" in cmd and "mpjpeg" in cmd
    assert cmd[-1].startswith("http://127.0.0.1:")
    # the poll view takes the camera back from the stream
    webcam.webcam_live_frame()
    assert fake["stopped"][-1] == webcam.STREAM_UNIT
    assert webcam.webcam_live_stop() == "Webcam live view stopped."
    assert not fake["units"]


def test_stream_refused_while_motion_watch_runs(fake, monkeypatch):
    monkeypatch.setattr(motion, "_active", lambda: True)
    with pytest.raises(ToolError, match="motion watch"):
        webcam.webcam_live_stream()


def test_stream_reports_camera_busy(fake, monkeypatch):
    import rose.tools.recorder as recorder

    monkeypatch.setattr(recorder, "_listening", lambda port: False)
    real_run = webcam.run

    def run_then_exit(cmd, **k):  # ffmpeg exits at once: the camera is in use
        real_run(cmd, **k)
        fake["units"].discard(webcam.STREAM_UNIT)
        return b""

    monkeypatch.setattr(webcam, "run", run_then_exit)
    with pytest.raises(ToolError, match="didn't start"):
        webcam.webcam_live_stream()


def test_capture_refuses_while_stream_holds_camera(fake):
    fake["units"].add(webcam.STREAM_UNIT)
    with pytest.raises(ToolError, match="live webcam video"):
        webcam.webcam_capture()


def test_screen_stream_command_and_click_mapping(monkeypatch):
    from rose.tools import recorder, screenshot

    cmds, units = [], set()
    monkeypatch.setattr(screenshot, "session_type", lambda: "x11")
    monkeypatch.setattr(screenshot, "resolve_area", lambda area, mon: ((1920, 0, 1920, 1080), "monitor 1"))
    monkeypatch.setattr(screenshot, "_screen_stream_active", lambda: screenshot.SCREEN_UNIT in units)
    monkeypatch.setattr(screenshot, "run", lambda cmd, **k: cmds.append(cmd) or units.add(screenshot.SCREEN_UNIT) or b"")
    monkeypatch.setattr(recorder, "_listening", lambda port: port == screenshot.SCREEN_PORT)
    info = screenshot.screen_live_stream(monitor=1, quality="medium")
    assert (info["width"], info["height"], info["scale"], info["offset_x"]) == (1600, 900, 1.2, 1920)
    cmd = cmds[0]
    assert cmd[cmd.index("-i") + 1].endswith("+1920,0") and cmd[cmd.index("-video_size") + 1] == "1920x1080"
    vf = cmd[cmd.index("-vf") + 1]
    assert vf.startswith("mpdecimate=max=20,") and "scale=1600:900" in vf and vf.endswith("format=yuvj444p")
    assert cmd[-1] == f"http://127.0.0.1:{screenshot.SCREEN_PORT}/screen.mjpg" and "-listen_timeout" in cmd
    with pytest.raises(ToolError, match="quality"):
        screenshot.screen_live_stream(quality="4k")


def test_mic_pcm_args_are_low_delay():
    from rose.tools import recorder

    args = recorder._listen_args("mic0", "pcm")
    assert args[args.index("-f", 3) + 1] == "mulaw" and "16000" in args and args[-1].endswith("/live.ulaw")
    assert args[:2] == ["-fragment_size", "1280"]
    assert recorder._listen_args("mic0", "webm")[-1].endswith("/live.webm")


def test_motion_watch_pipes_detection_to_a_second_ffmpeg(fake, monkeypatch, tmp_path):
    cmds = []
    md = tmp_path / "motion"
    monkeypatch.setattr(motion, "MOTION_DIR", md)
    monkeypatch.setattr(motion, "STATE", md / "state.json")
    monkeypatch.setattr(motion, "LOG", md / "events.log")
    monkeypatch.setattr(motion, "FRAME", md / "motion.jpg")
    monkeypatch.setattr(motion, "_active", lambda: False)
    monkeypatch.setattr(motion, "require", lambda *a: "ffmpeg", raising=False)
    monkeypatch.setattr(motion, "run", lambda cmd, **k: cmds.append(cmd) or b"")
    monkeypatch.setattr(webcam, "list_cameras", lambda: CAM)
    motion.motion_watch_start(sensitivity="medium", hours=1)
    script = cmds[-1][cmds[-1].index("-c") + 1]
    reader, detector = script.split(" | ")
    assert "/dev/video0" in reader and "-f nut -" in reader and "nobuffer" not in reader
    assert "fps=6,scale=960:-2" in reader and "1280x720" in reader
    assert detector.startswith("ffmpeg") and "-f nut -i -" in detector and "scene" in detector
