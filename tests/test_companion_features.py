"""Desktop Rose add-ons that run without a display: files to the phone, Claude/CI state, hotkey parsing,
push-to-talk trigger."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from rose import companion, hotkey


# ---------------- files to the phone

def test_parse_uri_list(tmp_path):
    a = tmp_path / "my file.pdf"
    a.write_text("x")
    b = tmp_path / "b.txt"
    b.write_text("y")
    payload = f"copy\nfile://{str(a).replace(' ', '%20')}\r\n# comment\nfile://{b}\nfile://otherhost/etc/passwd\n"
    assert companion.parse_uri_list(payload) == [str(a), str(b)]
    assert companion.parse_uri_list(str(b)) == [str(b)]  # a plain path copied as text
    assert companion.parse_uri_list(f"file://{tmp_path}") == []  # folders are not sent
    assert companion.parse_uri_list("hello world\nhttps://example.com/x") == []
    assert companion.parse_uri_list(f"file://{tmp_path}/missing.txt") == []


def test_clipboard_files_tries_targets(monkeypatch, tmp_path):
    f = tmp_path / "a.png"
    f.write_text("x")
    seen = []

    def fake_out(cmd, timeout=2):
        seen.append(cmd[4])
        return f"copy\nfile://{f}" if cmd[4] == "x-special/gnome-copied-files" else ""

    monkeypatch.setattr(companion, "_out", fake_out)
    assert companion.clipboard_files() == [str(f)]
    assert seen == ["text/uri-list", "x-special/gnome-copied-files"]


def test_latest_download(tmp_path):
    assert companion.latest_download(tmp_path) is None
    old, new = tmp_path / "old.zip", tmp_path / "new.pdf"
    old.write_text("1")
    new.write_text("2")
    os.utime(old, (time.time() - 100, time.time() - 100))
    (tmp_path / "busy.pdf.crdownload").write_text("3")
    (tmp_path / ".hidden").write_text("4")
    (tmp_path / "folder").mkdir()
    assert companion.latest_download(tmp_path) == str(new)
    assert companion.latest_download(tmp_path / "nope") is None


class _Handler(BaseHTTPRequestHandler):
    calls: list = []
    status = 200

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.calls.append((self.path, self.headers.get("Authorization"), body))
        self.send_response(_Handler.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True} if _Handler.status == 200 else {"detail": "path not allowed"}).encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def backend(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Handler.calls, _Handler.status = [], 200
    monkeypatch.setattr(companion, "_backend", lambda: (f"http://127.0.0.1:{srv.server_port}", "tok123"))
    yield _Handler
    srv.shutdown()


def test_push_file_and_errors(backend, tmp_path, monkeypatch):
    ok, msg = companion.push_file("/home/x/Downloads/a.pdf")
    assert ok and "a.pdf" in msg
    assert backend.calls == [("/api/files/push", "Bearer tok123", {"path": "/home/x/Downloads/a.pdf"})]
    backend.status = 403
    ok, msg = companion.push_file("/etc/shadow")
    assert not ok and "path not allowed" in msg
    monkeypatch.setattr(companion, "_backend", lambda: ("http://127.0.0.1:9", "tok"))
    ok, msg = companion.push_file("/x/y")
    assert not ok and "rose-phone" in msg
    monkeypatch.setattr(companion, "_backend", lambda: ("http://127.0.0.1:9", ""))
    assert companion.push_file("/x/y")[0] is False


def test_send_files(backend, monkeypatch, tmp_path):
    q: queue.Queue = queue.Queue()
    monkeypatch.setattr(companion, "clipboard_files", lambda: [])
    companion.send_files("clipboard", q)
    kind, (text, ok) = q.get_nowait()
    assert kind == "notice" and not ok and "clipboard" in text.lower()
    monkeypatch.setattr(companion, "latest_download", lambda: "/d/new.pdf")
    companion.send_files("download", q)
    assert q.get_nowait()[1][1] is True
    assert backend.calls[-1][2] == {"path": "/d/new.pdf"}


# ---------------- Claude Code / CI state

def test_system_state_rules():
    now = 1_800_000_000.0
    st = companion.system_state
    assert st(None, None, now)["claude"] == "idle"
    busy = {"state": "busy", "project": "rose-MCP", "session_id": "s1", "ts": now - 30}
    assert st(busy, {}, now)["claude"] == "busy"
    assert st({**busy, "ts": now - 31 * 60}, {}, now)["claude"] == "idle"  # older than 30 min
    assert st({**busy, "state": "done", "ts": now - 60}, {}, now)["claude"] == "done"
    assert st({**busy, "state": "done", "ts": now - 121}, {}, now)["claude"] == "idle"  # done is news for 2 min
    assert st({**busy, "state": "waiting", "ts": now - 600}, {}, now)["claude"] == "waiting"
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 10))
    assert st({**busy, "ts": iso}, {}, now)["claude"] == "busy"
    assert st({**busy, "ts": (now - 10) * 1000}, {}, now)["claude"] == "busy"  # milliseconds
    assert st({**busy, "state": "weird"}, {}, now)["claude"] == "idle"
    assert st({**busy, "ts": None}, {}, now)["claude"] == "idle"
    assert st(None, {"ci_failing": ["o/r#1"]}, now)["ci_failing"] is True
    assert st(None, {"ci_failing": []}, now)["ci_failing"] is False
    assert st("garbage", "garbage", now)["claude"] == "idle"  # type: ignore[arg-type]


def test_bubble_on_changes_only():
    now = 1_800_000_000.0
    w = companion.system_state({"state": "waiting", "project": "rose", "session_id": "a", "ts": now}, {}, now)
    assert companion.bubble_for(None, w) == ("surprised", "Claude needs you in rose")
    assert companion.bubble_for(w, w) is None  # same event: no repeat
    d = companion.system_state({"state": "done", "project": "", "session_id": "a", "ts": now + 5}, {}, now + 5)
    assert companion.bubble_for(w, d) == ("proud", "Claude finished")
    b = companion.system_state({"state": "busy", "session_id": "a", "ts": now}, {}, now)
    assert companion.bubble_for(d, b) is None
    assert companion.bubble_for(d, companion.system_state(None, None, now)) is None


def test_read_system_state_files(tmp_path, monkeypatch):
    monkeypatch.setattr(companion, "CLAUDE_STATE", tmp_path / "c.json")
    monkeypatch.setattr(companion, "WATCH_STATE", tmp_path / "w.json")
    assert companion.read_system_state()["claude"] == "idle"
    (tmp_path / "c.json").write_text(json.dumps({"state": "busy", "project": "p", "ts": time.time()}))
    (tmp_path / "w.json").write_text("{not json")
    s = companion.read_system_state()
    assert s["claude"] == "busy" and s["ci_failing"] is False


# ---------------- hotkey

def test_hotkey_parse():
    assert hotkey.parse("super+r") == (["Mod4Mask"], "r")
    assert hotkey.parse("Ctrl+Alt+R") == (["ControlMask", "Mod1Mask"], "r")
    assert hotkey.parse("f9") == ([], "f9")
    for bad in ("", "super+", "hyper+r", "ctrl+shift"):
        with pytest.raises(ValueError):
            hotkey.parse(bad)


def test_lock_variants():
    assert hotkey.lock_variants([2, 16]) == [0, 2, 16, 18]
    assert hotkey.lock_variants([2, 16, 16, 0]) == [0, 2, 16, 18]
    assert len(hotkey.lock_variants([2, 16, 32])) == 8


def test_hotkey_spec_and_gnome(settings, monkeypatch):
    from rose import config

    real = config.load_config
    assert companion.hotkey_spec() == "ctrl+alt+r"
    monkeypatch.setattr(companion, "load_config", lambda: {**real(), "face_hotkey": "OFF"})
    assert companion.hotkey_spec() == ""
    monkeypatch.setattr(companion, "load_config", lambda: {**real(), "face_hotkey": "Ctrl+Alt+R"})
    assert companion.hotkey_spec() == "ctrl+alt+r"
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    monkeypatch.setattr(companion, "_out", lambda cmd, timeout=2: "'Super'")
    assert companion.gnome_keeps_super()
    monkeypatch.setattr(companion, "_out", lambda cmd, timeout=2: "''")
    assert not companion.gnome_keeps_super()
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    assert not companion.gnome_keeps_super()


def test_trigger_talk(monkeypatch):
    sent, started = [], []
    monkeypatch.setattr(companion, "_running_pid", lambda: 4242)
    monkeypatch.setattr(companion.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(companion, "start_detached", lambda: started.append(1))
    import signal

    assert companion.trigger_talk() == 0 and sent == [(4242, signal.SIGUSR1)] and not started
    monkeypatch.setattr(companion, "_running_pid", lambda: None)
    companion.trigger_talk()
    assert started == [1] and companion._state().get("talk_on_start") is True
