"""phone.py internals: device pick cache, auto-reconnect after a drop, Unicode typing. No real adb."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from rose.common import ToolError
from rose.tools import phone as P


@pytest.fixture
def adb(monkeypatch, tmp_path):
    """Fake `adb`: script maps a command prefix to (returncode, output); every call is recorded."""
    calls: list[list[str]] = []
    script: dict[tuple, list] = {}

    def run(cmd, capture_output=True, timeout=None):
        calls.append(cmd)
        args = tuple(cmd[3:] if cmd[1:2] == ["-s"] else cmd[1:])
        for prefix, outs in script.items():
            if args[:len(prefix)] == prefix:
                rc, out = outs.pop(0) if len(outs) > 1 else outs[0]
                return SimpleNamespace(returncode=rc, stdout=out.encode(), stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(P.subprocess, "run", run)
    monkeypatch.setattr(P, "require", lambda *a: "adb")
    monkeypatch.setattr(P, "_server_started", True)
    monkeypatch.setattr(P, "_serial_cache", None)
    monkeypatch.setattr(P, "_last_reconnect", 0.0)
    monkeypatch.setattr(P, "LAST_FILE", tmp_path / "phone_last.json")
    monkeypatch.setattr(P.time, "sleep", lambda s: None)
    monkeypatch.setattr(P, "_tailscale_phone_ip", lambda: "")  # no real Tailscale in tests
    dead: set[str] = set()  # addresses whose TCP probe fails
    monkeypatch.setattr(P, "_alive", lambda addr, timeout=None: addr not in dead)
    monkeypatch.setattr(P, "ROSE_HOME", tmp_path)
    return SimpleNamespace(calls=calls, script=script, dead=dead)


def _n(calls, *prefix):
    return sum(1 for c in calls if tuple(c[1:1 + len(prefix)]) == prefix)


def test_serial_pick_is_cached(adb):
    adb.script[("devices",)] = [(0, "List of devices attached\n100.1.2.3:5555\tdevice\nR58M\tdevice\n")]
    for _ in range(3):
        P._shell("echo", "hi")
    assert _n(adb.calls, "devices") == 1
    assert all(c[1:3] == ["-s", "R58M"] for c in adb.calls if "shell" in c)  # USB preferred


def test_connect_saves_address_and_reconnects_after_drop(adb):
    adb.script[("connect",)] = [(0, "connected to 100.1.2.3:4444")]
    P.phone_connect("100.1.2.3:4444")
    assert P._load_last() == "100.1.2.3:4444"
    adb.script[("shell",)] = [(1, "adb: no devices/emulators found"), (0, "ok")]
    assert P._shell("echo", "hi") == "ok"
    assert _n(adb.calls, "connect", "100.1.2.3:4444") == 2


def test_reconnect_finds_new_port_over_mdns(adb):
    P._save_last("192.168.1.5:4444")
    adb.script[("connect", "192.168.1.5:4444")] = [(1, "failed to connect to 192.168.1.5:4444")]
    adb.script[("mdns",)] = [(0, "List of discovered mdns services\n"
                                 "adb-R58M-x\t_adb-tls-connect._tcp\t192.168.1.5:39001\n")]
    adb.script[("connect", "192.168.1.5:39001")] = [(0, "connected to 192.168.1.5:39001")]
    adb.script[("shell",)] = [(1, "error: device offline"), (0, "ok")]
    assert P._shell("echo") == "ok"
    assert P._load_last() == "192.168.1.5:39001"


def test_no_reconnect_loop(adb):
    P._save_last("100.1.2.3:4444")
    adb.script[("connect",)] = [(1, "failed to connect")]
    adb.script[("shell",)] = [(1, "adb: no devices/emulators found")]
    for _ in range(3):
        with pytest.raises(ToolError, match="No phone connected"):
            P._shell("echo")
    assert _n(adb.calls, "connect") == 1  # one attempt, then rate-limited


def test_no_reconnect_without_saved_address(adb):
    adb.script[("shell",)] = [(1, "adb: no devices/emulators found")]
    with pytest.raises(ToolError, match="phone_connect"):
        P._shell("echo")
    assert _n(adb.calls, "connect") == 0


def test_connect_enables_remote_over_tailscale(adb, monkeypatch):
    monkeypatch.setattr(P, "_tailscale_phone_ip", lambda: "100.9.9.9")
    adb.dead.add("100.9.9.9:5555")  # not on yet
    adb.script[("connect", "192.168.1.2:45031")] = [(0, "connected to 192.168.1.2:45031")]
    adb.script[("tcpip",)] = [(0, "restarting in TCP mode port: 5555")]
    adb.script[("connect", "192.168.1.2:5555")] = [(0, "connected to 192.168.1.2:5555")]
    msg = P.phone_connect("192.168.1.2:45031")
    assert "100.9.9.9:5555" in msg
    assert P._load_last() == "192.168.1.2:5555"
    assert P._load_remote() == "100.9.9.9:5555"


def test_connect_without_tailscale_skips_tcpip(adb):
    adb.script[("connect",)] = [(0, "connected to 192.168.1.2:45031")]
    msg = P.phone_connect("192.168.1.2:45031")
    assert msg.startswith("Phone connected at 192.168.1.2:45031.") and "not on: Phone not found on Tailscale" in msg
    assert not any("tcpip" in c for c in adb.calls)
    assert P._load_remote() == ""


def test_reconnect_falls_back_to_tailscale(adb):
    P._save_last(address="192.168.1.2:5555", remote="100.9.9.9:5555")
    adb.script[("connect", "192.168.1.2:5555")] = [(1, "failed to connect to '192.168.1.2:5555': No route to host")]
    adb.script[("connect", "100.9.9.9:5555")] = [(0, "connected to 100.9.9.9:5555")]
    adb.script[("shell",)] = [(1, "adb: no devices/emulators found"), (0, "ok")]
    assert P._shell("echo") == "ok"
    assert P._load_last() == "192.168.1.2:5555"  # LAN address kept for home


def test_stale_wifi_link_is_dropped_and_reconnects_over_tailscale(adb):
    """Phone left Wi-Fi: adb still lists it as "device"; ROSE drops it and uses the Tailscale fallback."""
    P._save_last(address="192.168.1.2:41893", remote="100.9.9.9:5555")
    adb.dead.add("192.168.1.2:41893")
    adb.script[("devices",)] = [(0, "List of devices attached\n192.168.1.2:41893\tdevice\n"),
                                (0, "List of devices attached\n100.9.9.9:5555\tdevice\n")]
    adb.script[("connect", "100.9.9.9:5555")] = [(0, "connected to 100.9.9.9:5555")]
    adb.script[("shell",)] = [(0, "ok")]
    assert P._shell("echo") == "ok"
    assert _n(adb.calls, "disconnect", "192.168.1.2:41893") == 1
    assert _n(adb.calls, "connect", "192.168.1.2:41893") == 0  # dead address skipped, no 15 s hang


def test_offline_entry_is_dropped(adb):
    adb.script[("devices",)] = [(0, "List of devices attached\n192.168.1.2:41893\toffline\n")]
    assert P._pick_serial() == P.NO_PHONE
    assert _n(adb.calls, "disconnect", "192.168.1.2:41893") == 1


def test_reconnect_tries_tailscale_without_saved_remote(adb, monkeypatch):
    monkeypatch.setattr(P, "_tailscale_phone_ip", lambda: "100.9.9.9")
    P._save_last(address="192.168.1.2:41893")
    adb.dead.add("192.168.1.2:41893")
    adb.script[("connect", "100.9.9.9:5555")] = [(0, "connected to 100.9.9.9:5555")]
    assert P._reconnect()
    assert P._load_remote() == "100.9.9.9:5555"


def test_enable_remote_records_why_it_failed(adb, monkeypatch):
    monkeypatch.setattr(P, "_tailscale_phone_ip", lambda: "100.9.9.9")
    adb.dead.add("100.9.9.9:5555")
    adb.script[("tcpip",)] = [(1, "error: closed")]
    assert P._enable_remote("192.168.1.2:41893") == ""
    assert "tcpip 5555 failed: error: closed" in P._read_last()["remote_error"]


def test_enable_remote_skips_tcpip_when_port_already_open(adb, monkeypatch):
    monkeypatch.setattr(P, "_tailscale_phone_ip", lambda: "100.9.9.9")
    assert P._enable_remote("192.168.1.2:41893") == "100.9.9.9:5555"
    assert not any("tcpip" in c for c in adb.calls)


def test_link_status_and_keepalive(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(P, "_tailscale_phone_ip", lambda: "100.9.9.9")
    P._save_last(address="192.168.1.2:41893")
    adb.dead.add("100.9.9.9:5555")
    adb.script[("devices",)] = [(0, "List of devices attached\n192.168.1.2:41893\tdevice\n")]
    adb.script[("tcpip",)] = [(0, "restarting in TCP mode port: 5555")]
    adb.script[("connect", "192.168.1.2:5555")] = [(0, "connected to 192.168.1.2:5555")]
    st = P.keepalive()
    assert st["via"] == "wifi"
    assert _n(adb.calls, "-s", "192.168.1.2:41893", "tcpip") == 1
    assert json.loads((tmp_path / "phone_link.json").read_text())["via"] == "wifi"
    P.keepalive()  # no second tcpip within REMOTE_RETRY
    assert _n(adb.calls, "-s", "192.168.1.2:41893", "tcpip") == 1


def test_keepalive_rescans_moved_port(adb, monkeypatch):
    import rose.phone_qr as Q
    P._save_last(address="192.168.1.2:41893")
    adb.dead.add("192.168.1.2:41893")
    monkeypatch.setattr(P, "_host_up", lambda a: True)
    monkeypatch.setattr(P, "_last_scan", 0.0)
    monkeypatch.setattr(P.time, "monotonic", lambda: 10_000.0)
    monkeypatch.setattr(Q, "find_port", lambda ip, skip=None: 38001)
    adb.script[("connect", "192.168.1.2:38001")] = [(0, "connected to 192.168.1.2:38001")]
    P.keepalive()
    assert P._load_last() == "192.168.1.2:38001"


def test_keepalive_skips_scan_when_phone_off_lan(adb, monkeypatch):
    import rose.phone_qr as Q
    P._save_last(address="192.168.1.2:41893")
    adb.dead.add("192.168.1.2:41893")
    monkeypatch.setattr(P, "_host_up", lambda a: False)
    monkeypatch.setattr(Q, "find_port", lambda *a, **k: pytest.fail("scanned while phone is away"))
    assert P.keepalive()["via"] == "offline"


def test_keepalive_prefers_lan_over_tailscale(adb):
    P._save_last(address="192.168.1.2:5555", remote="100.100.9.9:5555")
    adb.script[("devices",)] = [(0, "List of devices attached\n100.100.9.9:5555\tdevice\n"),
                                (0, "List of devices attached\n100.100.9.9:5555\tdevice\n"),
                                (0, "List of devices attached\n100.100.9.9:5555\tdevice\n"),
                                (0, "List of devices attached\n192.168.1.2:5555\tdevice\n")]
    adb.script[("connect", "192.168.1.2:5555")] = [(0, "connected to 192.168.1.2:5555")]
    assert P.keepalive()["via"] == "wifi"
    assert _n(adb.calls, "disconnect", "100.100.9.9:5555") == 1


def test_keepalive_without_phone_does_nothing(adb):
    assert P.keepalive()["never_connected"]
    assert adb.calls == []


def test_via():
    assert P._via("R58M") == "usb"
    assert P._via("192.168.1.2:5555") == "wifi"
    assert P._via("100.102.41.115:5555") == "tailscale"


def test_tailscale_phone_ip_parses_status(monkeypatch):
    status = {"Peer": {"a": {"OS": "linux", "TailscaleIPs": ["100.1.1.1"], "Online": True},
                       "b": {"OS": "android", "TailscaleIPs": ["fd7a::1", "100.102.41.115"], "Online": True}}}
    monkeypatch.setattr(P.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(status).encode()))
    assert P._tailscale_phone_ip() == "100.102.41.115"


def test_type_ascii_uses_input_text(adb):
    P.phone_type("on my way")
    assert any(c[-1] == "input text on%smy%sway" for c in adb.calls)


def test_type_unicode_needs_adbkeyboard(adb):
    adb.script[("shell", "ime list -s")] = [(0, "com.google.android.inputmethod.latin/.LatinIME")]
    with pytest.raises(ToolError, match="ADBKeyboard"):
        P.phone_type("नमस्ते 👋")


def test_type_unicode_via_adbkeyboard_restores_keyboard(adb):
    gboard = "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"
    adb.script[("shell", "ime list -s")] = [(0, f"{gboard}\n{P.ADB_IME}")]
    adb.script[("shell", "settings get secure default_input_method")] = [(0, gboard)]
    P.phone_type("100% done ✅")
    shells = [c[-1] for c in adb.calls if "shell" in c]
    assert any(s.startswith("am broadcast -a ADB_INPUT_B64") for s in shells)
    assert shells[-1] == f"ime set {gboard}"
