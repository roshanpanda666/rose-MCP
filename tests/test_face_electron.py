"""The Electron face host (rose/face_electron.py): its stdout protocol, the face choice and its commands.
No display or Electron needed: the child process is replaced by a pipe we read."""

from __future__ import annotations

import io
import json
from pathlib import Path

from rose import companion, face_electron


def test_parse_command_only_marked_lines():
    assert face_electron.parse_command(face_electron.MARK + '{"cmd": "ptt"}') == {"cmd": "ptt"}
    assert face_electron.parse_command('{"cmd": "ptt"}') is None  # unmarked: a Node/Chromium log line
    assert face_electron.parse_command(face_electron.MARK + "not json") is None
    assert face_electron.parse_command(face_electron.MARK + '{"nocmd": 1}') is None
    assert face_electron.parse_command(face_electron.MARK + "[1, 2]") is None


def test_wanted_follows_state_and_launcher(monkeypatch, tmp_path):
    state = {}
    monkeypatch.setattr(companion, "_state", lambda: state)
    monkeypatch.setattr(face_electron, "launcher", lambda: tmp_path / "rose-face")
    assert face_electron.wanted()
    state["face_ui"] = "tk"
    assert not face_electron.wanted()
    state["face_ui"] = "electron"
    monkeypatch.setattr(face_electron, "launcher", lambda: None)  # Electron not installed: classic face
    assert not face_electron.wanted()


def test_launcher_needs_electron(monkeypatch, tmp_path):
    cmd = tmp_path / "rose-face"
    cmd.write_text("#!/bin/sh\n")
    monkeypatch.setenv("ROSE_FACE_CMD", str(cmd))
    assert face_electron.launcher() is None
    (tmp_path / "node_modules" / "electron").mkdir(parents=True)
    assert face_electron.launcher() == cmd


class _Proc:
    def __init__(self):
        self.stdin = io.BytesIO()

    def poll(self):
        return 0  # already exited, so quit()'s force-kill thread does nothing

    def terminate(self):
        pass

    def sent(self) -> list[dict]:
        return [json.loads(x) for x in self.stdin.getvalue().decode().splitlines()]


def _host():
    h = face_electron.Host(Path("/nonexistent"))
    h.proc = _Proc()
    return h


def test_save_only_known_keys(monkeypatch):
    saved = {}
    monkeypatch.setattr(companion, "_save_state", lambda **kw: saved.update(kw))
    h = _host()
    h.handle({"cmd": "save", "values": {"speak": True, "face_pos": [10, 20], "evil": 1, "face_ui": "x"}})
    assert saved == {"speak": True, "face_pos": [10, 20]}


def test_state_and_send(monkeypatch):
    h = _host()
    h.handle({"cmd": "state", "busy": True, "talking": False})
    assert h.busy and not h.talking
    h.send("ctx", mood="idle", status="hanging out")
    assert h.proc.sent() == [{"ev": "ctx", "mood": "idle", "status": "hanging out"}]


def test_ptt_toggle_cancels(monkeypatch):
    h = _host()
    started = []
    monkeypatch.setattr(h, "_ptt_run", lambda stop, gen: started.append(gen))
    h.hotkey_pressed()
    assert h.ptt is not None
    h.hotkey_pressed()  # again: cancel
    assert h.ptt is None
    states = [m["state"] for m in h.proc.sent() if m["ev"] == "ptt"]
    assert states == ["listening", "cancelled"]


def test_classic_switch_restarts_tk(monkeypatch):
    saved = {}
    monkeypatch.setattr(companion, "_save_state", lambda **kw: saved.update(kw))
    h = _host()
    h.handle({"cmd": "classic"})
    assert saved == {"face_ui": "tk"} and h.restart and h.stopping
    assert h.proc.sent()[-1] == {"ev": "quit"}


def test_send_files_notice(monkeypatch):
    def fake_send(source, q):
        q.put(("notice", (f"sent {source}", True)))

    monkeypatch.setattr(companion, "send_files", fake_send)
    h = _host()
    h._send_files("clipboard")
    assert h.proc.sent() == [{"ev": "notice", "text": "sent clipboard", "ok": True}]


def test_look_clean_clamps_and_drops():
    from rose import face_look

    got = face_look.clean({"size": 9, "speed": "x", "gaze": 0, "hair_hue": 400, "eye_hue": "", "skin": "green",
                           "voice": "groq:hannah", "evil": 1})
    assert got["size"] == 1.6 and got["speed"] == 1.0 and got["gaze"] is False
    assert got["hair_hue"] == 40 and got["eye_hue"] is None and got["skin"] == "default"
    assert got["voice"] == "groq:hannah" and "evil" not in got


def test_look_save_merges(monkeypatch):
    from rose import face_look

    state = {"look": {"size": 0.8}}
    monkeypatch.setattr(companion, "_state", lambda: state)
    monkeypatch.setattr(companion, "_save_state", lambda **kw: state.update(kw))
    look = face_look.save({"hair_hue": 200})
    assert look["size"] == 0.8 and look["hair_hue"] == 200 and state["look"] == look


def test_look_view_and_menu_command(monkeypatch):
    from rose import face_look

    assert face_look.clean({"view": "full"})["view"] == "full"
    assert face_look.clean({"view": "legs"})["view"] == "bust"
    state = {}
    monkeypatch.setattr(companion, "_state", lambda: state)
    monkeypatch.setattr(companion, "_save_state", lambda **kw: state.update(kw))
    _host().handle({"cmd": "look", "changes": {"view": "face"}})
    assert state["look"]["view"] == "face"
