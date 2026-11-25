"""phone_guard: location parsing, sound routing (unlocked -> player, locked -> alarm), confirm gates. No real adb."""

from __future__ import annotations

import pytest

from rose import guard
from rose.common import ToolError
from rose.tools import phone_guard as G

DUMP = """      last location (fine): {fused, 21.3600001,83.6900001±100.0m, ert=11-25 02:16:14.91}
          fused: {fused, 21.3600002,83.6900002±20.0m, ert=11-25 02:12:06.87}
          gps: {gps, 21.3600003,83.6900003±9.3m, alt=74.29±69.3m, spd=.0±2.7m/s, ert=11-25 01:34:11.81}
NOW 2026 11-25 02:16:30"""


@pytest.fixture
def adb(monkeypatch, tmp_path):
    calls: list[str] = []
    replies: dict[str, str] = {}

    def fake(*args, timeout=20, binary=False, _retry=True):
        cmd = " ".join(args)
        calls.append(cmd)
        for key, out in replies.items():
            if key in cmd:
                return out
        return ""

    monkeypatch.setattr(G.phone, "_adb", fake)
    monkeypatch.setattr(G, "OUT_DIR", tmp_path)
    monkeypatch.setattr(G.time, "sleep", lambda s: None)
    return calls, replies


def test_location_picks_newest_fix(adb):
    calls, replies = adb
    replies["dumpsys activity service com.google.android.gms"] = DUMP
    replies["is-location-enabled"] = "true"
    loc = G.phone_location()
    assert (loc["lat"], loc["accuracy_m"], loc["age_s"]) == (21.3600001, 100.0, 16)
    assert loc["maps"] == "https://maps.google.com/?q=21.3600001,83.6900001"


def test_location_turns_location_on(adb):
    calls, replies = adb
    replies["dumpsys activity service com.google.android.gms"] = DUMP
    replies["is-location-enabled"] = "false"
    assert "switched it on" in G.phone_location()["note"]
    assert any("set-location-enabled true" in c for c in calls)


def test_location_without_fix_fails(adb):
    adb[1]["is-location-enabled"] = "true"
    with pytest.raises(ToolError, match="no location fix"):
        G.phone_location()


def test_speak_and_power_need_confirm(adb):
    with pytest.raises(ToolError, match="confirm=true"):
        G.phone_speak(text="hi")
    with pytest.raises(ToolError, match="confirm=true"):
        G.phone_power("poweroff")
    with pytest.raises(ToolError, match="confirm=true"):
        G.phone_lost_mode()
    assert adb[0] == []


def test_siren_uses_timer_at_full_alarm_volume(adb):
    calls, replies = adb
    replies["--stream 4 --get"] = "volume is 3 in range [1..16]"
    G.phone_speak(siren=True, confirm=True)
    assert any("--stream 4 --set 16" in c for c in calls)
    assert any("SET_TIMER" in c and "LENGTH 1" in c for c in calls)


def test_locked_phone_plays_as_alarm(adb, monkeypatch):
    calls, replies = adb
    replies["content query"] = "Row: 0 _id=42"
    replies["mWakefulness"] = "mWakefulness=Asleep"
    uri_msg = G.play_on_phone(b"mp3", volume=50)
    assert "alarm" in uri_msg
    alarm = next(c for c in calls if "SET_ALARM" in c)
    assert "RINGTONE content://media/external/audio/media/42" in alarm and "MESSAGE ROSE" in alarm


def test_unlocked_phone_plays_now(adb):
    calls, replies = adb
    replies["content query"] = "Row: 0 _id=42"
    replies["mWakefulness"] = "isKeyguardShowing=false\nmWakefulness=Awake"
    assert G.play_on_phone(b"mp3", volume=None) == "Playing on the phone now."
    assert any("AudioPreviewActivity" in c and "media/42" in c for c in calls)
    assert not any("SET_ALARM" in c for c in calls)


def test_remote_clients_need_approval():
    for name in ("phone_location", "phone_photo", "phone_mic_record", "phone_lost_mode"):
        assert name in guard.ALWAYS_APPROVE
    for name in ("phone_speak", "phone_power", "phone_stop_sound"):
        assert name in guard.GUI_TOOLS


class _Sock:
    def __init__(self, data: bytes = b""):
        self.data, self.sent = data, b""

    def recv(self, n):
        out, self.data = self.data[:n], self.data[n:]
        return out

    def sendall(self, b):
        self.sent += b


def test_screen_packets_add_aud_and_track_size():
    import struct
    scr = G.ScrcpyScreen()
    frame = b"\x00\x00\x00\x01\x65abc"
    cfg = b"\x00\x00\x00\x01\x67sps"
    scr.video = _Sock(struct.pack(">III", 0x80000000, 494, 1100)
                      + struct.pack(">QI", 1 << 62, len(cfg)) + cfg
                      + struct.pack(">QI", 1234, len(frame)) + frame)
    out = list(scr.packets())
    assert scr.size == (494, 1100)
    assert out == [cfg, frame + G.AUD]  # config as is, frames flushed with an AUD


def test_screen_control_messages():
    import struct
    scr = G.ScrcpyScreen()
    scr.size = (494, 1100)
    scr.control = _Sock()
    scr.touch("down", 600, -5)  # clamped into the video
    msg = scr.control.sent
    assert len(msg) == 32
    t, act, pid, x, y, w, h, pressure, ab, buttons = struct.unpack(">BBqiiHHHii", msg)
    assert (t, act, pid, x, y, w, h, pressure) == (2, 0, -2, 493, 0, 494, 1100, 0xFFFF)
    scr.control.sent = b""
    scr.key("back")
    assert scr.control.sent == struct.pack(">BBiii", 0, 0, 4, 0, 0) + struct.pack(">BBiii", 0, 1, 4, 0, 0)
    scr.control.sent = b""
    scr.text("hi")
    assert scr.control.sent == b"\x01\x00\x00\x00\x02hi"
