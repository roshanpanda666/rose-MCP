"""Motion watch: event log reading by offset, frames only for the client that started the watch."""

from __future__ import annotations

import json

from rose import guard
from rose.tools import motion

LOG = """frame:0    pts:8       pts_time:4
lavfi.scene_score=0.051000
frame:1    pts:9       pts_time:4.5
lavfi.scene_score=0.120000
"""


def _setup(owner="phone-app"):
    motion.MOTION_DIR.mkdir(parents=True, exist_ok=True)
    motion.LOG.write_text(LOG)
    motion.FRAME.write_bytes(b"\xff\xd8\xff\xe0jpeg\xff\xd9")
    motion.STATE.write_text(json.dumps({"owner": owner, "sensitivity": "medium", "until": 1}))


def test_events_by_offset(monkeypatch):
    _setup()
    monkeypatch.setattr(motion, "_active", lambda: True)
    monkeypatch.setattr(guard, "caller", lambda: "Forge")  # local Claude Code
    res = motion.motion_events(0)
    meta = json.loads(res[0])
    assert meta["new"] == 2 and meta["max_score"] == 0.12 and meta["watching"] is True
    assert res[1].data.startswith(b"\xff\xd8")
    again = motion.motion_events(meta["offset"])
    assert again["new"] == 0 and "image" not in again
    with motion.LOG.open("a") as f:
        f.write("frame:2    pts:10      pts_time:5\nlavfi.scene_score=0.0")  # partial line: wait for it
    part = motion.motion_events(meta["offset"])
    assert part["new"] == 0  # the score line isn't finished yet
    with motion.LOG.open("a") as f:
        f.write("9\n")
    done = json.loads(motion.motion_events(part["offset"])[0])
    assert done["new"] == 1 and done["max_score"] == 0.09


def test_frames_only_for_owner(monkeypatch):
    _setup(owner="phone-app")
    monkeypatch.setattr(motion, "_active", lambda: True)
    monkeypatch.setattr(guard, "caller", lambda: "claude.ai")
    other = motion.motion_events(0)
    assert isinstance(other, dict) and other["new"] == 2 and other["image"].startswith("withheld")
    monkeypatch.setattr(guard, "caller", lambda: "phone-app")
    assert isinstance(motion.motion_events(0), list)


def test_approved_call_runs_for_the_requester(monkeypatch):
    seen = []
    token = guard._acting_for.set("phone-app")
    try:
        monkeypatch.setattr(guard, "caller", lambda: "Forge")
        seen.append(guard.effective_caller())
    finally:
        guard._acting_for.reset(token)
    assert seen == ["phone-app"] and guard.effective_caller() == "Forge"


def test_stop_cleans_up(monkeypatch):
    _setup()
    monkeypatch.setattr(motion, "_active", lambda: False)
    assert "wasn't running" in motion.motion_watch_stop()
    assert not motion.LOG.exists() and not motion.FRAME.exists() and not motion.STATE.exists()


def test_echo_cancel_load_and_unload(monkeypatch):
    from rose.tools import recorder

    calls = []

    def fake_text(cmd, timeout=10):
        calls.append(cmd)
        if cmd[:3] == ["pactl", "list", "short"]:
            return "12\tmodule-echo-cancel\taec_method=webrtc source_name=rose_ec_source sink_name=rose_ec_sink\n" \
                   "3\tmodule-null-sink\tsink_name=other"
        if cmd[:2] == ["pactl", "get-default-sink"]:
            return "speakers"
        if cmd[:2] == ["pactl", "load-module"]:
            return "77"
        return ""

    monkeypatch.setattr(recorder, "run_text", fake_text)
    monkeypatch.setattr(recorder, "run", lambda cmd, timeout=15, **k: calls.append(cmd) or b"")
    assert recorder._ec_load("mic1") == "77"
    assert ["pactl", "unload-module", "12"] in calls  # an old one is removed first; module 3 is left alone
    assert ["pactl", "unload-module", "3"] not in calls
    load = next(c for c in calls if c[:2] == ["pactl", "load-module"])
    assert "source_master=mic1" in load and "sink_master=speakers" in load
