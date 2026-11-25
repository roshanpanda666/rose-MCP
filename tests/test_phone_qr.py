from rose import phone_qr

AVAHI = """+;wlan0;IPv4;rose-abc;_adb-tls-pairing._tcp;local
=;wlan0;IPv6;rose-abc;_adb-tls-pairing._tcp;local;Pixel.local;fe80::1;37111;
=;wlan0;IPv4;rose-abc;_adb-tls-pairing._tcp;local;Pixel.local;192.168.1.20;37111;
=;wlan0;IPv4;adb-XYZ-1;_adb-tls-connect._tcp;local;Pixel.local;192.168.1.20;41234;
"""


def test_payload():
    assert phone_qr.payload("rose-x", "pw") == "WIFI:T:ADB;S:rose-x;P:pw;;"


def test_parse_avahi_ipv4_only_and_service():
    assert phone_qr.parse_avahi(AVAHI, "_adb-tls-pairing._tcp") == [{"name": "rose-abc", "ip": "192.168.1.20", "port": 37111}]
    assert phone_qr.parse_avahi(AVAHI, "_adb-tls-connect._tcp")[0]["port"] == 41234


def test_qr_svg():
    svg = phone_qr.qr_svg(phone_qr.payload("rose-x", "pw"))
    assert svg.lstrip().startswith("<?xml") or "<svg" in svg


def test_find_port_local():
    import socket
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen()
    port = srv.getsockname()[1]
    try:
        assert phone_qr.find_port("127.0.0.1", lo=port - 5, hi=port + 5) == port
        assert phone_qr.find_port("127.0.0.1", skip={port}, lo=port, hi=port + 1) is None
    finally:
        srv.close()


def test_connect_accepts_port_only_pairing(monkeypatch):
    from rose.tools import phone
    calls = []
    monkeypatch.setattr(phone, "_adb", lambda *a, **k: calls.append(a) or "connected to x")
    monkeypatch.setattr(phone, "_save_last", lambda *a, **k: None)
    monkeypatch.setattr(phone, "_enable_remote", lambda a: "")
    phone.phone_connect("192.168.1.2:45031", "339842", "36683")
    assert calls[0] == ("pair", "192.168.1.2:36683", "339842")
