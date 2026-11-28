"""The Electron face host (rose/face_electron.py): its stdout protocol, the face choice and its commands.
No display or Electron needed: the child process is replaced by a pipe we read."""

from __future__ import annotations

import io
import json
import threading
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
    assert saved == {"_push": True, "speak": True, "face_pos": [10, 20]}
    saved.clear()  # a value that came from the phone (synced) is saved without posting it back
    h.handle({"cmd": "save", "values": {"caveman": False}, "synced": True})
    assert saved == {"_push": False, "caveman": False}


def test_caveman_change_is_posted_once(monkeypatch, tmp_path):
    monkeypatch.setattr(companion, "STATE_FILE", tmp_path / "companion.json")
    posts = []
    monkeypatch.setattr(companion, "push_caveman", lambda: posts.append(companion._state()["caveman"]))

    class T:  # run the post inline
        def __init__(self, target, daemon=None):
            self.t = target

        def start(self):
            self.t()

    monkeypatch.setattr(companion.threading, "Thread", T)
    companion._save_state(caveman=False)
    companion._save_state(caveman=False)               # unchanged: no post
    companion._save_state(_push=False, caveman=True)   # from the phone: no echo
    companion._save_state(speak=True)                  # other keys: no post
    assert posts == [False]


def test_clap_switch_syncs_with_the_phone(monkeypatch, tmp_path):
    import io
    import json as _json

    monkeypatch.setattr(companion, "STATE_FILE", tmp_path / "companion.json")
    monkeypatch.setattr(companion, "_backend", lambda: ("http://phone.test", "tok"))
    posts, server = [], {"on": None}

    class T:  # run the post inline
        def __init__(self, target, daemon=None):
            self.t = target

        def start(self):
            self.t()

    def urlopen(req, timeout=None):
        if req.get_method() == "POST":
            posts.append(_json.loads(req.data)["on"])
            server["on"] = posts[-1]
        return io.BytesIO(_json.dumps({"on": server["on"]}).encode())

    monkeypatch.setattr(companion.threading, "Thread", T)
    monkeypatch.setattr(companion.urllib.request, "urlopen", urlopen)
    companion.pull_clap()  # the server has none yet: ours (off) is shared
    assert posts == [False]
    companion._save_state(clap=True)  # flipped in the dashboard: posted
    assert posts == [False, True]
    server["on"] = False  # a stale value right after our change doesn't undo it: ours is sent again
    companion.pull_clap()
    assert companion._state()["clap"] is True and posts[-1] is True
    monkeypatch.setattr(companion.time, "time", lambda: 10 ** 10)  # later, the phone turns it off
    server["on"] = False
    companion.pull_clap()
    assert companion._state()["clap"] is False and posts == [False, True, True]


def test_state_and_send(monkeypatch):
    h = _host()
    h.handle({"cmd": "state", "busy": True, "talking": False})
    assert h.busy and not h.talking
    h.send("ctx", mood="idle", status="hanging out")
    assert h.proc.sent() == [{"ev": "ctx", "mood": "idle", "status": "hanging out"}]


def test_turn_end_keeps_her_voice_talking(monkeypatch):
    """The turn's idle message (no "talking") must not end barge-in while her voice still plays."""
    monkeypatch.setattr(companion, "barge_wanted", lambda: False)
    h = _host()
    h.handle({"cmd": "state", "busy": True, "talking": True})
    h.handle({"cmd": "state", "busy": False})
    assert not h.busy and h.talking
    h.handle({"cmd": "state", "busy": False, "talking": False})
    assert not h.talking


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


def test_hide_keeps_her_listening_in_the_background(monkeypatch):
    """Closing Rose's window hides both windows; the host (wake word, hotkey) stays alive."""
    state = {"wheel": False}
    monkeypatch.setattr(companion, "_state", lambda: state)
    monkeypatch.setattr(companion, "_save_state", lambda **kw: state.update(kw))
    monkeypatch.setattr(companion, "stop_wheel", lambda: None)
    h = _host()
    h.handle({"cmd": "hide"})
    assert h.hidden and not h.stopping and state["hidden"] is True
    assert h.proc.sent() == [{"ev": "background"}]
    h.handle({"cmd": "hide"})  # again: nothing new
    assert h.proc.sent() == [{"ev": "background"}]
    h.handle({"cmd": "show"})
    assert not h.hidden and state["hidden"] is False
    assert h.proc.sent()[-1] == {"ev": "show"}


def test_apply_visibility_follows_saved_state(monkeypatch):
    state = {"hidden": True, "wheel": False}
    monkeypatch.setattr(companion, "_state", lambda: state)
    monkeypatch.setattr(companion, "_save_state", lambda **kw: state.update(kw))
    monkeypatch.setattr(companion, "stop_wheel", lambda: None)
    h = _host()
    h.apply_visibility()  # `rose face --hide`
    assert h.hidden
    state["hidden"] = False
    h.apply_visibility()  # `rose face` again
    assert not h.hidden
    assert [m["ev"] for m in h.proc.sent()] == ["background", "show"]


def test_wake_and_ptt_bring_her_back(monkeypatch):
    state = {"wheel": False}
    monkeypatch.setattr(companion, "_state", lambda: state)
    monkeypatch.setattr(companion, "_save_state", lambda **kw: state.update(kw))
    monkeypatch.setattr(companion, "stop_wheel", lambda: None)
    h = _host()
    h.handle({"cmd": "hide"})
    h._woken()  # "Hey Rose" while she's away
    assert not h.hidden
    assert [m["ev"] for m in h.proc.sent()] == ["background", "show", "wake"]
    h.handle({"cmd": "hide"})
    monkeypatch.setattr(h, "_ptt_run", lambda stop, gen: None)
    h.hotkey_pressed()
    assert not h.hidden


def test_quit_is_the_only_real_stop(monkeypatch):
    monkeypatch.setattr(companion, "_save_state", lambda **kw: None)
    h = _host()
    h.handle({"cmd": "quit"})
    assert h.stopping and h.proc.sent()[-1] == {"ev": "quit"}


def test_trigger_hide_and_show_signal_a_running_rose(monkeypatch):
    import os
    import signal

    state: dict = {}
    killed: list = []
    monkeypatch.setattr(companion, "_state", lambda: state)
    monkeypatch.setattr(companion, "_save_state", lambda **kw: state.update(kw))
    monkeypatch.setattr(companion, "_running_pid", lambda: 4242)
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert companion.trigger_hide() == 0 and state["hidden"] is True
    assert companion.trigger_show() == 0 and state["hidden"] is False
    assert killed == [(4242, signal.SIGUSR2), (4242, signal.SIGUSR2)]
    monkeypatch.setattr(companion, "_running_pid", lambda: None)
    assert companion.trigger_hide() == 1 and companion.trigger_show() == 1


def test_electron_windows_hide_instead_of_quitting():
    """ROSE Desktop and Rose's face keep running when their window is closed."""
    desktop = Path(face_electron.__file__).resolve().parents[1] / "desktop"
    if not desktop.is_dir():
        return  # installed copy without the Electron apps
    app = (desktop / "main.js").read_text()
    assert 'win.on("close"' in app and "win.hide()" in app
    assert "window-all-closed" in app and "if (!tray) app.quit()" not in app
    face = (desktop / "face" / "main.js").read_text()
    assert 'face.on("close"' in face and "closedByUser()" in face
    assert 'app.on("window-all-closed", () => app.quit())' not in face


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


def _quiet_wheel(monkeypatch) -> dict:
    """The menu wheel is a whole other Electron: never start one in a test."""
    calls = {"start": 0, "stop": 0}
    monkeypatch.setattr(companion, "start_wheel", lambda *a, **k: calls.update(start=calls["start"] + 1))
    monkeypatch.setattr(companion, "stop_wheel", lambda *a, **k: calls.update(stop=calls["stop"] + 1))
    return calls


class _Ears:
    """Stands in for a running WakeListener."""

    def __init__(self, alive=True):
        self.alive, self.stopped = alive, False

    def is_alive(self):
        return self.alive

    def stop(self):
        self.stopped = True


def test_hide_closes_her_away_but_keeps_listening(monkeypatch):
    state = {"wheel": True}
    monkeypatch.setattr(companion, "_state", lambda: state)
    monkeypatch.setattr(companion, "_save_state", lambda **kw: state.update(kw))
    wheel = _quiet_wheel(monkeypatch)
    h = _host()
    ears = h.wake = _Ears()
    h.handle({"cmd": "hide"})  # the window was closed
    assert h.hidden and state["hidden"] is True
    assert not ears.stopped and h.wake is ears  # her ears stay open: the mic is still hers
    assert wheel["stop"] == 1 and wheel["start"] == 0
    assert h.proc.sent()[-1] == {"ev": "background"}
    h.handle({"cmd": "hide"})  # again: nothing more to do
    assert wheel["stop"] == 1


def test_show_brings_her_back_with_the_wheel(monkeypatch):
    state = {"wheel": True, "hidden": True}
    monkeypatch.setattr(companion, "_state", lambda: state)
    monkeypatch.setattr(companion, "_save_state", lambda **kw: state.update(kw))
    wheel = _quiet_wheel(monkeypatch)
    h = _host()
    h.hidden = True
    h.handle({"cmd": "show"})
    assert not h.hidden and state["hidden"] is False
    assert h.proc.sent()[-1] == {"ev": "show"}
    for t in threading.enumerate():  # show() starts the wheel off-thread
        if t is not threading.current_thread():
            t.join(timeout=2)
    assert wheel["start"] == 1


def test_apply_visibility_follows_the_saved_choice(monkeypatch):
    state = {"wheel": False, "hidden": True}
    monkeypatch.setattr(companion, "_state", lambda: state)
    monkeypatch.setattr(companion, "_save_state", lambda **kw: state.update(kw))
    _quiet_wheel(monkeypatch)
    h = _host()
    h.apply_visibility()  # `rose face --hide`
    assert h.hidden
    state["hidden"] = False
    h.apply_visibility()  # `rose face` again
    assert not h.hidden


def test_quit_is_the_only_thing_that_stops_her(monkeypatch):
    _quiet_wheel(monkeypatch)
    h = _host()
    h.handle({"cmd": "quit"})
    assert h.stopping and h.proc.sent()[-1] == {"ev": "quit"}


def test_wake_guard_starts_her_ears_again(monkeypatch):
    from rose import wake

    state = {"wake": True}
    monkeypatch.setattr(companion, "_state", lambda: state)
    monkeypatch.setattr(wake, "available", lambda: True)
    started = []
    h = _host()
    monkeypatch.setattr(h, "_wake_start", lambda: started.append(h.wake))
    h.wake = _Ears(alive=True)
    h._wake_guard()
    assert started == []  # still listening: leave her be
    h.wake = _Ears(alive=False)  # the thread died (a Vosk or audio error)
    h._wake_guard()
    assert started == [None] and h.wake is None  # cleared, then started again
    state["wake"] = False  # she was asked not to listen: stay quiet
    h.wake = None
    h._wake_guard()
    assert started == [None]


def test_double_clap_keeps_her_ears_open_and_closes_them_when_both_are_off(monkeypatch):
    from rose import wake

    state = {"wake": False, "clap": True}
    monkeypatch.setattr(companion, "_state", lambda: state)
    monkeypatch.setattr(wake, "available", lambda: True)
    started = []
    h = _host()
    monkeypatch.setattr(h, "_wake_start", lambda: started.append(1))
    h._wake_guard()
    assert started == [1] and h.ears == (False, True)  # the clap alone needs the mic
    ears = h.wake = _Ears()
    state["clap"] = False  # both off: the mic closes
    h._wake_guard()
    assert ears.stopped and h.wake is None and h.ears == (False, False)


def test_wake_listener_claps_without_the_wake_word(monkeypatch):
    """A double clap runs on_clap even with "Hey Rose" off (Vosk never loads)."""
    from rose import wake

    claps = []
    pcm = _double_clap_pcm()
    chunks = [pcm[i:i + wake.CHUNK * 2] for i in range(0, len(pcm), wake.CHUNK * 2)]

    class Mic:
        opens = 1

        def __init__(self, *a, **k):
            pass

        def read(self, n):
            return chunks.pop(0) if chunks else b""

        def shutdown(self):
            pass

    monkeypatch.setattr(wake, "MicStream", Mic)
    monkeypatch.setattr(wake, "available", lambda: False)
    w = wake.WakeListener(on_wake=None, on_command=None, paused=lambda: False, on_clap=lambda: claps.append(1),
                          clap_on=lambda: True, wake_on=lambda: False)
    w.run()
    assert claps == [1]


def _double_clap_pcm() -> bytes:
    from tests.test_wake import clap_audio

    return clap_audio([0.5, 0.85])


def test_look_view_and_menu_command(monkeypatch):
    from rose import face_look

    assert face_look.clean({"view": "full"})["view"] == "full"
    assert face_look.clean({"view": "legs"})["view"] == "bust"
    state = {}
    monkeypatch.setattr(companion, "_state", lambda: state)
    monkeypatch.setattr(companion, "_save_state", lambda **kw: state.update(kw))
    _host().handle({"cmd": "look", "changes": {"view": "face"}})
    assert state["look"]["view"] == "face"


def test_face_ears_replace_host_barge_in(monkeypatch):
    """Once the face has its own echo-cancelled mic, the host doesn't start wake.BargeIn; if the face's mic
    fails, the host's barge-in takes over again."""
    from rose import wake

    started = []

    class FakeBarge:
        triggered = False

        def __init__(self, **kw):
            started.append(self)

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(wake, "BargeIn", FakeBarge)
    monkeypatch.setattr(companion, "barge_wanted", lambda: True)
    monkeypatch.setattr(companion, "_log", lambda m: None)
    h = _host()
    h.handle({"cmd": "ears", "on": True})
    h.handle({"cmd": "state", "busy": False, "talking": True})
    assert started == [] and h.barge is None
    h.handle({"cmd": "ears", "on": False, "error": "NotAllowedError"})
    assert len(started) == 1 and h.barge is started[0]


def test_face_barge_audio_is_transcribed(monkeypatch):
    import base64

    got = []
    done = threading.Event()
    monkeypatch.setattr(companion, "barge_wanted", lambda: True)

    def transcribe(wav):
        got.append(wav)
        done.set()
        return "Hey Rose, open the calendar"

    monkeypatch.setattr(companion, "transcribe", transcribe)
    h = _host()
    h.face_ears = True
    h.handle({"cmd": "barge_start"})
    h.handle({"cmd": "barge_audio", "wav": base64.b64encode(b"RIFFdata").decode()})
    assert done.wait(2)
    for _ in range(50):
        if any(m.get("ev") == "voice_cmd" for m in h.proc.sent()):
            break
        threading.Event().wait(0.02)
    sent = h.proc.sent()
    assert got == [b"RIFFdata"]
    assert {"ev": "barge", "state": "paused"} in sent and {"ev": "barge", "state": "stop"} in sent
    assert sent[-1]["ev"] == "voice_cmd" and "calendar" in sent[-1]["text"]


def test_face_barge_audio_garbage_resumes_her(monkeypatch):
    h = _host()
    h.handle({"cmd": "barge_audio", "wav": "!!not base64!!"})
    assert h.proc.sent() == [{"ev": "barge", "state": "resume"}]


def test_ptt_streams_the_recording_to_the_face(monkeypatch):
    """Push-to-talk sends voice_begin, the audio as it is recorded, then voice_end (the face transcribes)."""
    import base64

    from rose import wake

    def record(stop, on_chunk=None, **kw):
        on_chunk(b"ab")
        on_chunk(b"cd")
        return b"RIFF"

    monkeypatch.setattr(wake, "push_to_talk", record)
    h = _host()
    h.ptt_gen = 1
    h.ptt = {"stop": threading.Event(), "gen": 1}
    h._ptt_run(h.ptt["stop"], 1)
    sent = h.proc.sent()
    assert [m["ev"] for m in sent] == ["voice_begin", "voice_pcm", "voice_pcm", "voice_end"]
    assert sent[0]["source"] == "ptt" and len({m["id"] for m in sent}) == 1
    assert b"".join(base64.b64decode(m["pcm"]) for m in sent[1:3]) == b"abcd"
    assert sent[-1]["cancelled"] is False and h.ptt is None


def test_ptt_cancel_drops_the_stream(monkeypatch):
    from rose import wake

    h = _host()
    h.ptt_gen = 1
    h.ptt = {"stop": threading.Event(), "gen": 1}

    def record(stop, on_chunk=None, **kw):
        h.hotkey_pressed()  # pressed again while talking: cancel
        return b""

    monkeypatch.setattr(wake, "push_to_talk", record)
    h._ptt_run(h.ptt["stop"], 1)
    assert h.proc.sent()[-1] == {"ev": "voice_end", "id": 1, "cancelled": True, "error": ""}


def test_barge_voice_done_listens_again_only_after_a_cough(monkeypatch):
    calls = []
    h = _host()
    monkeypatch.setattr(h, "_barge_follow", lambda: calls.append(1))

    class _B:
        triggered = True

    h.barge = _B()
    h.handle({"cmd": "voice_done", "source": "barge", "heard": True})
    assert h.barge is None and calls == []  # new command: her voice stops, so no listening over her
    h.barge = _B()
    h.handle({"cmd": "voice_done", "source": "barge", "heard": False})
    assert h.barge is None and calls == [1]
