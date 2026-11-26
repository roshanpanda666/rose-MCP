"""Fingerprint (WebAuthn passkey) approvals: real ES256 keys, real authData / clientData / CBOR."""

from __future__ import annotations

import hashlib
import json
import struct

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from rose import fingerprint as fp
from rose import guard, physkey, webauthn
from rose.common import ToolError

BASE = "https://rose.example.test"
RP_ID = "rose.example.test"
ORIGIN = BASE
UP, UV, AT = 0x01, 0x04, 0x40


# ---------------- a tiny CBOR encoder + fake authenticator ----------------

def cbor(v) -> bytes:
    def head(mt, n):
        if n < 24:
            return bytes([mt << 5 | n])
        for ai, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 1 << (8 * size):
                return bytes([mt << 5 | ai]) + n.to_bytes(size, "big")
        raise ValueError
    if isinstance(v, bool):
        return b"\xf5" if v else b"\xf4"
    if v is None:
        return b"\xf6"
    if isinstance(v, int):
        return head(0, v) if v >= 0 else head(1, -1 - v)
    if isinstance(v, bytes):
        return head(2, len(v)) + v
    if isinstance(v, str):
        b = v.encode()
        return head(3, len(b)) + b
    if isinstance(v, list):
        return head(4, len(v)) + b"".join(cbor(x) for x in v)
    if isinstance(v, dict):
        return head(5, len(v)) + b"".join(cbor(k) + cbor(x) for k, x in v.items())
    raise TypeError(v)


class Authenticator:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        nums = self.key.public_key().public_numbers()
        self.cose = cbor({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})
        self.cred_id = b"cred-" + __import__("os").urandom(16)
        self.count = 0

    def create(self, challenge, origin=ORIGIN, rp_id=RP_ID, flags=UP | UV | AT, typ="webauthn.create"):
        cd = json.dumps({"type": typ, "challenge": challenge, "origin": origin}).encode()
        ad = (hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", 0) + bytes(16)
              + struct.pack(">H", len(self.cred_id)) + self.cred_id + self.cose)
        att = cbor({"fmt": "none", "attStmt": {}, "authData": ad})
        return {"clientDataJSON": webauthn.b64u(cd), "attestationObject": webauthn.b64u(att)}

    def get(self, aid, challenge, count=None, origin=ORIGIN, flags=UP | UV, bad_sig=False):
        self.count = self.count + 1 if count is None else count
        cd = json.dumps({"type": "webauthn.get", "challenge": challenge, "origin": origin}).encode()
        ad = hashlib.sha256(RP_ID.encode()).digest() + bytes([flags]) + struct.pack(">I", self.count)
        sig = self.key.sign(ad + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        if bad_sig:
            sig = self.key.sign(b"something else", ec.ECDSA(hashes.SHA256()))
        return {"id": aid, "credentialId": webauthn.b64u(self.cred_id), "clientDataJSON": webauthn.b64u(cd),
                "authenticatorData": webauthn.b64u(ad), "signature": webauthn.b64u(sig)}


# ---------------- fixtures ----------------

@pytest.fixture
def phone(monkeypatch):
    """Control the phone lock state: phone('unlocked'|'locked'|'unknown')."""
    state = {"s": "unlocked"}
    monkeypatch.setattr(fp, "_probe_lock", lambda: state["s"])

    def set_state(s):
        state["s"] = s
        fp.set_lock_state(s, "test")
    set_state("unlocked")
    return set_state


@pytest.fixture
def base(monkeypatch, auth_state, phone):
    monkeypatch.setattr(fp, "public_base", lambda: BASE)
    monkeypatch.setattr(fp, "NOTIFY_COALESCE", 1000.0)
    for p in (fp.PASSKEY_PATH, fp.WINDOWS, fp.STREAMS, fp.LOST_FLAG):
        p.unlink(missing_ok=True)
    guard._approve_failures.clear()
    guard._rate.clear()
    guard.end_grant("test")
    yield auth_state[0]
    fp.flush_notifications()
    for p in (fp.PASSKEY_PATH, fp.WINDOWS, fp.STREAMS, fp.LOST_FLAG, fp.LOCK_STATE):
        p.unlink(missing_ok=True)
    guard._approve_failures.clear()
    guard.set_lockdown(False)


def _register(passphrase, authn=None, **create_kw):
    authn = authn or Authenticator()
    url = fp.new_registration("phone")
    tok = url.split("t=", 1)[1]
    opts = fp.registration_options(tok, passphrase)
    res = fp.registration_finish(tok, authn.create(opts["challenge"], **create_kw))
    return authn, res


@pytest.fixture
def fpmode(base, settings, monkeypatch):
    """Fingerprint mode on, one registered passkey, remote caller 'Claude (test)'."""
    authn, _ = _register(base)
    settings(approval_mode="fingerprint", fingerprint_window_minutes=5)
    who = {"c": "Claude (test)"}
    monkeypatch.setattr(guard, "caller", lambda: who["c"])
    return {"authn": authn, "passphrase": base, "who": who}


def _counter():
    calls = []

    def tool(path: str = "x") -> str:
        calls.append(path)
        return f"read {path}"
    return calls, tool


def _approve(authn, aid, **kw):
    rec = guard.load_approval(aid)
    return fp.approve_webauthn(aid, authn.get(aid, rec["challenge"], **kw))


# ---------------- registration ----------------

def test_registration_ok(base):
    _, res = _register(base)
    keys = fp.list_passkeys()
    assert res["name"] == "phone" and len(keys) == 1 and keys[0]["alg"] == -7
    assert oct(fp.PASSKEY_PATH.stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("kw, why", [
    ({"origin": "https://evil.example"}, "origin"),
    ({"rp_id": "evil.example"}, "rpIdHash"),
    ({"flags": UP | AT}, "verification"),
    ({"typ": "webauthn.get"}, "type"),
])
def test_registration_rejects_bad_ceremony(base, kw, why):
    with pytest.raises(PermissionError, match=why):
        _register(base, **kw)
    assert fp.list_passkeys() == []


def test_registration_rejects_wrong_challenge_and_passphrase(base):
    tok = fp.new_registration("phone").split("t=", 1)[1]
    with pytest.raises(PermissionError, match="Wrong passphrase"):
        fp.registration_options(tok, "nope")
    fp.registration_options(tok, base)
    with pytest.raises(PermissionError, match="challenge"):
        fp.registration_finish(tok, Authenticator().create("not-the-challenge"))
    with pytest.raises(LookupError):  # the challenge was used up by the failed attempt
        fp.registration_finish(tok, Authenticator().create("x"))


def test_enable_refuses_without_passkey(base, settings):
    assert fp.cli(["enable"]) == 1
    assert fp.mode() == "passphrase"


# ---------------- assertions ----------------

def test_assertion_runs_parked_call_once(fpmode):
    calls, fn = _counter()
    tool = guard.guarded("read_file", fn, read_only=True)
    res = tool("a.txt")
    assert res["status"] == "approval_required" and calls == []
    rec = _approve(fpmode["authn"], res["approval_id"])
    assert rec["status"] == "done" and calls == ["a.txt"]
    with pytest.raises(LookupError):
        _approve(fpmode["authn"], res["approval_id"])
    assert calls == ["a.txt"] and guard.check_approval(res["approval_id"])["status"] == "done"


def test_replayed_or_foreign_challenge_rejected(fpmode):
    a = fpmode["authn"]
    tool = guard.guarded("phone_call", lambda number="1": "called", read_only=False)
    r1, r2 = tool(), tool()
    old = guard.load_approval(r1["approval_id"])["challenge"]
    with pytest.raises(PermissionError, match="signature"):
        fp.approve_webauthn(r1["approval_id"], a.get(r1["approval_id"], old, bad_sig=True))
    with pytest.raises(PermissionError, match="challenge"):  # the failed attempt used the challenge up
        fp.approve_webauthn(r1["approval_id"], a.get(r1["approval_id"], old))
    assertion = a.get(r1["approval_id"], guard.load_approval(r1["approval_id"])["challenge"])
    assertion2 = dict(assertion, id=r2["approval_id"])
    with pytest.raises(PermissionError, match="challenge"):  # r1's signature can't approve r2
        fp.approve_webauthn(r2["approval_id"], assertion2)
    assert fp.approve_webauthn(r1["approval_id"], assertion)["status"] == "done"


def test_sign_count_regression_rejected(fpmode):
    a = fpmode["authn"]
    tool = guard.guarded("phone_message", lambda to="x": "sent", read_only=False)
    assert _approve(a, tool()["approval_id"], count=5)["status"] == "done"
    with pytest.raises(PermissionError, match="cloned"):
        _approve(a, tool()["approval_id"], count=3)


def test_uv_flag_and_unknown_credential_rejected(fpmode):
    tool = guard.guarded("phone_call", lambda: "called", read_only=False)
    with pytest.raises(PermissionError, match="verification"):
        _approve(fpmode["authn"], tool()["approval_id"], flags=UP)
    with pytest.raises(PermissionError, match="unknown passkey"):
        _approve(Authenticator(), tool()["approval_id"])


def test_failures_count_toward_lockout(fpmode):
    tool = guard.guarded("phone_call", lambda: "called", read_only=False)
    aid = tool()["approval_id"]
    for _ in range(5):
        with pytest.raises(PermissionError):
            _approve(fpmode["authn"], aid, bad_sig=True)
    with pytest.raises(PermissionError, match="Too many"):
        _approve(fpmode["authn"], aid)


def test_passphrase_cannot_approve_fingerprint_record(fpmode):
    tool = guard.guarded("read_file", lambda path="x": "text", read_only=True)
    aid = tool()["approval_id"]
    with pytest.raises(PermissionError, match="fingerprint"):
        guard.approve(aid, fpmode["passphrase"])
    assert guard.load_approval(aid)["status"] == "pending"


# ---------------- windows, fresh tools, phone lock ----------------

def test_window_is_per_caller_and_skips_fresh_tools(fpmode):
    calls, fn = _counter()
    read = guard.guarded("read_file", fn, read_only=True)
    call = guard.guarded("phone_call", lambda: "called", read_only=False)
    _approve(fpmode["authn"], read("1")["approval_id"])
    assert fp.window_active("Claude (test)")
    assert read("2") == {"_untrusted_content_notice": guard.UNTRUSTED_NOTE, "content": "read 2"}
    assert call()["status"] == "approval_required"  # fresh: always a new fingerprint
    fpmode["who"]["c"] = "Other client"
    assert read("3")["status"] == "approval_required"  # another caller doesn't share the window
    assert calls == ["1", "2"]


def test_fresh_tool_never_opens_window(fpmode):
    cam = guard.guarded("webcam_capture", lambda: "photo", read_only=False)
    assert _approve(fpmode["authn"], cam()["approval_id"])["status"] == "done"
    assert not fp.window_active("Claude (test)")
    assert cam()["status"] == "approval_required"


def test_phone_locked_denies_instantly(fpmode, phone):
    phone("locked")
    tool = guard.guarded("read_file", lambda path="x": "text", read_only=True)
    before = set(guard.APPROVAL_DIR.glob("*.json"))
    with pytest.raises(ToolError, match="locked"):
        tool()
    assert set(guard.APPROVAL_DIR.glob("*.json")) == before  # nothing parked, nothing pushed


def test_unknown_state_never_uses_window(fpmode):
    read = guard.guarded("read_file", lambda path="x": "text", read_only=True)
    _approve(fpmode["authn"], read()["approval_id"])
    fp._write(fp.LOCK_STATE, {"state": "unknown", "ts": __import__("time").time()})
    assert fp.window_active("Claude (test)")
    assert read()["status"] == "approval_required"


def test_lock_transition_ends_window(fpmode, phone):
    read = guard.guarded("read_file", lambda path="x": "text", read_only=True)
    _approve(fpmode["authn"], read()["approval_id"])
    phone("locked")
    assert not fp.window_active("Claude (test)")
    phone("unlocked")
    assert read()["status"] == "approval_required"


def test_stale_state_is_reprobed(fpmode, phone, monkeypatch):
    phone("unlocked")
    fp._write(fp.LOCK_STATE, {"state": "unlocked", "ts": 0})
    monkeypatch.setattr(fp, "_probe_lock", lambda: "locked")
    with pytest.raises(ToolError, match="locked"):
        guard.guarded("read_file", lambda: "x", read_only=True)()


def test_live_frames_follow_stream_ticket(fpmode, phone):
    start = guard.guarded("webcam_live_stream", lambda: {"stream": "on"}, read_only=False)
    frame = guard.guarded("webcam_live_frame", lambda: "frame", read_only=False)
    stop = guard.guarded("webcam_live_stop", lambda: "stopped", read_only=False)
    with pytest.raises(ToolError, match="webcam_live_stream first"):
        frame()
    _approve(fpmode["authn"], start()["approval_id"])
    assert frame()["content"] == "frame"
    fpmode["who"]["c"] = "Other client"
    with pytest.raises(ToolError):
        frame()
    fpmode["who"]["c"] = "Claude (test)"
    assert stop() == "stopped"  # switching off never needs a fingerprint
    with pytest.raises(ToolError):
        frame()
    _approve(fpmode["authn"], start()["approval_id"])
    phone("locked")
    with pytest.raises(ToolError, match="locked"):
        frame()
    phone("unlocked")
    with pytest.raises(ToolError, match="webcam_live_stream first"):  # the lock ended the ticket
        frame()


def test_lockdown_still_wins(fpmode):
    guard.set_lockdown(True, "test")
    with pytest.raises(ToolError, match="LOCKDOWN"):
        guard.guarded("write_file", lambda path="x", confirm=False: "w", read_only=False)("a", confirm=True)


# ---------------- callers, modes ----------------

def test_forge_exempt_unless_gate_local(fpmode, settings):
    fpmode["who"]["c"] = "Forge"
    tool = guard.guarded("take_screenshot", lambda: "shot", read_only=True)
    assert tool()["content"] == "shot"
    settings(fingerprint_gate_local=True)
    assert tool()["status"] == "approval_required"


def test_exempt_client_keeps_passphrase_rules(fpmode, settings):
    settings(fingerprint_exempt_clients=["Claude (test)"])
    assert guard.guarded("list_dir", lambda: "ls", read_only=True)()["content"] == "ls"
    res = guard.guarded("type_text", lambda text="": "typed", read_only=False)("hi")
    assert res["status"] == "approval_required"
    assert guard.load_approval(res["approval_id"]).get("mode") is None  # a normal passphrase record


def test_passphrase_mode_unchanged(base, settings, monkeypatch):
    monkeypatch.setattr(guard, "caller", lambda: "Claude (test)")
    assert fp.mode() == "passphrase"
    assert guard.guarded("list_dir", lambda: "ls", read_only=True)()["content"] == "ls"
    res = guard.guarded("type_text", lambda text="": "typed", read_only=False)("hi")
    assert "mode" not in guard.load_approval(res["approval_id"])
    assert guard.approve(res["approval_id"], base)["status"] == "done"
    guard.end_grant("test")


def test_unknown_mode_fails_closed(base, settings, monkeypatch):
    monkeypatch.setattr(guard, "caller", lambda: "Claude (test)")
    settings(approval_mode="fingerprnt")
    assert guard.guarded("type_text", lambda text="": "typed", read_only=False)("hi")["status"] == "approval_required"
    settings(approval_mode="chat")  # documented "chat" mode stays as it was
    assert guard.guarded("type_text", lambda text="": "typed", read_only=False)("hi") == "typed"


# ---------------- laptop fallback, lost-phone mode ----------------

def test_fallback_needs_passphrase_and_physical_key(fpmode, monkeypatch):
    pressed = {"ok": False, "key": None}

    def fake_wait(key, timeout):
        pressed["key"] = key
        return pressed["ok"]
    monkeypatch.setattr(physkey, "wait_for_key", fake_wait)
    calls, fn = _counter()
    aid = guard.guarded("read_file", fn, read_only=True)("f")["approval_id"]
    with pytest.raises(PermissionError, match="Wrong passphrase"):
        fp.fallback_start(aid, "wrong", wait=True)
    rec = fp.fallback_start(aid, fpmode["passphrase"], wait=True)  # no key press
    assert rec["status"] == "pending" and calls == [] and "fallback_error" in rec
    assert pressed["key"] in physkey.KEY_CODES
    pressed["ok"] = True
    rec = fp.fallback_start(aid, fpmode["passphrase"], wait=True)
    assert rec["status"] == "done" and calls == ["f"]
    assert not fp.window_active("Claude (test)")  # fallback approvals are one-shot


def test_lost_mode_ignores_lock_and_needs_fallback(fpmode, phone, monkeypatch):
    monkeypatch.setattr(physkey, "wait_for_key", lambda key, timeout: True)
    fp.set_lost(True)
    phone("locked")
    res = guard.guarded("read_file", lambda path="x": "text", read_only=True)()
    assert res["status"] == "approval_required"
    with pytest.raises(PermissionError, match="Lost-phone"):
        _approve(fpmode["authn"], res["approval_id"])
    assert fp.fallback_start(res["approval_id"], fpmode["passphrase"], wait=True)["status"] == "done"


def test_fingerprint_test_round_trip(fpmode):
    res = fp._new_approval(fp.TEST_TOOL, {}, "rose fingerprint test", fresh=True, test=True)
    rec = _approve(fpmode["authn"], res["approval_id"])
    assert rec["status"] == "done" and "round trip OK" in rec["result"]
    assert not fp.window_active("rose fingerprint test")


# ---------------- notifications ----------------

def test_notify_batches_calls_and_skips_frames(fpmode, monkeypatch):
    import rose.push
    sent = []
    monkeypatch.setattr(rose.push, "push", lambda title, message="", **k: sent.append((title, message, k)))
    fp.flush_notifications()
    sent.clear()
    for t in ("read_file", "list_dir", "read_file"):
        fp.note_call("Claude (test)", t)
    fp.note_call("Claude (test)", "screen_frame")
    assert sent == []  # coalesced, nothing yet
    fp.flush_notifications()
    assert len(sent) == 1 and sent[0][0].startswith("ROSE: 3 calls") and "read_file, list_dir" in sent[0][1]
    assert sent[0][2]["priority"] == "low"
    fp.note_call("Claude (test)", "list_dir")
    fp.flush_notifications()
    assert sent[-1][0] == "ROSE: Claude (test) ran list_dir"


# ---------------- CBOR / evdev units ----------------

@pytest.mark.parametrize("raw", [b"\xa1\x01\x02\x00", b"\x5f\x41\x00\xff", b"\xc0\x00", b"\xfb" + bytes(8),
                                 b"\xa2\x01\x01\x01\x02", b"\x5a\xff\xff\xff\xff"])
def test_cbor_strict(raw):
    with pytest.raises(webauthn.WebAuthnError):
        webauthn.cbor_decode(raw)


def test_cbor_roundtrip():
    v = {1: 2, -1: b"\x00", "a": [True, None, "x"]}
    assert webauthn.cbor_decode(cbor(v)) == v


def test_keyboards_skip_virtual_devices(tmp_path, monkeypatch):
    dev = tmp_path / "devices"
    real = ('I: Bus=0011 Vendor=0001 Product=0001 Version=ab83\nN: Name="AT Translated Set 2 keyboard"\n'
            'S: Sysfs=/devices/platform/i8042/serio0/input/input3\nH: Handlers=sysrq kbd leds event3 \n'
            'B: EV=120013\nB: KEY=402000000 3803078f800d001 feffffdfffefffff fffffffffffffffe\n')
    virt = ('I: Bus=0006 Vendor=0000 Product=0000 Version=0000\nN: Name="xdotool virtual keyboard"\n'
            'S: Sysfs=/devices/virtual/input/input30\nH: Handlers=sysrq kbd event30 \n'
            'B: EV=120013\nB: KEY=402000000 3803078f800d001 feffffdfffefffff fffffffffffffffe\n')
    mouse = ('I: Bus=0003 Vendor=1 Product=2 Version=1\nN: Name="Mouse"\nS: Sysfs=/devices/pci0/usb1/input/input9\n'
             'H: Handlers=mouse0 event9 \nB: EV=17\nB: KEY=1f0000 0 0 0 0\n')
    dev.write_text(real + "\n" + virt + "\n" + mouse)
    monkeypatch.setattr(physkey, "DEVICES", dev)
    assert physkey.keyboards(physkey.KEY_CODES["K"]) == ["/dev/input/event3"]


# ---------------- HTTP pages ----------------

def test_approval_page_and_webauthn_route(fpmode, monkeypatch):
    from starlette.testclient import TestClient

    from rose import server
    client = TestClient(server.build_http_app("http://127.0.0.1:8765", 8765), base_url="http://127.0.0.1:8765")
    tool = guard.guarded("read_file", lambda path="x": "text", read_only=True)  # after the real tools registered
    aid = tool()["approval_id"]
    r = client.get(f"{guard.APPROVE_PATH}?id={aid}")
    assert r.status_code == 200 and "Approve with fingerprint" in r.text and "Phone dead or lost?" in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'nonce-" in csp and "frame-ancestors 'none'" in csp and r.headers["cache-control"] == "no-store"
    r = client.post(guard.APPROVE_PATH, data={"id": aid, "passphrase": fpmode["passphrase"], "action": "approve"})
    assert r.status_code == 401 and guard.load_approval(aid)["status"] == "pending"
    rec = guard.load_approval(aid)
    r = client.post(fp.WEBAUTHN_PATH, json=fpmode["authn"].get(aid, rec["challenge"]))
    assert r.status_code == 200 and r.json()["status"] == "done"


# ---------------- Rose app (Android Keystore key, fingerprint per use) ----------------

class AppKey:
    """Stands in for the Rose Voice app's Keystore key: P-256, SHA256withECDSA over device_message()."""

    def __init__(self):
        from cryptography.hazmat.primitives import serialization
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.spki = webauthn.b64u(self.key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))
        self.kid = ""

    def register(self, passphrase, device="A142"):
        tok = fp.new_registration("phone").split("t=", 1)[1]
        res = fp.device_register(tok, passphrase, self.spki, device)
        self.kid = res["key_id"]
        return res

    def sign(self, aid, challenge=None, msg=None):
        challenge = challenge or fp.device_options(aid)["challenge"]
        sig = self.key.sign(msg or fp.device_message(aid, challenge), ec.ECDSA(hashes.SHA256()))
        return {"id": aid, "key_id": self.kid, "challenge": challenge, "signature": webauthn.b64u(sig)}


@pytest.fixture
def appmode(fpmode):
    k = AppKey()
    k.register(fpmode["passphrase"])
    return {**fpmode, "app": k}


def test_app_registration_needs_link_and_passphrase(base):
    k = AppKey()
    tok = fp.new_registration("phone").split("t=", 1)[1]
    with pytest.raises(PermissionError, match="Wrong passphrase"):
        fp.device_register(tok, "nope", k.spki, "A142")
    with pytest.raises(LookupError):
        fp.device_register("bogus", base, k.spki, "A142")
    res = fp.device_register(tok, base, k.spki, "A142")
    assert res["name"] == "app-A142" and fp.has_app_key() and fp.status()["passkeys"] == ["app-A142"]
    with pytest.raises(LookupError):  # one use
        fp.device_register(tok, base, k.spki, "A142")
    assert fp.remove_passkey("app-A142") == 1 and not fp.has_app_key()


def test_app_registration_rejects_non_p256(base):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec as ec_
    other = ec_.generate_private_key(ec_.SECP384R1()).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    tok = fp.new_registration("phone").split("t=", 1)[1]
    for bad in (webauthn.b64u(other), "!!", webauthn.b64u(b"junk")):
        with pytest.raises(PermissionError):
            fp.device_register(tok, base, bad)
    assert not fp.has_app_key()


def test_app_signature_runs_parked_call_once(appmode):
    calls, fn = _counter()
    tool = guard.guarded("read_file", fn, read_only=True)
    aid = tool("a.txt")["approval_id"]
    opts = fp.device_options(aid)
    assert opts["tool"] == "read_file" and opts["caller"] == "Claude (test)" and opts["window_minutes"] == 5
    rec = fp.approve_device(aid, appmode["app"].sign(aid))
    assert rec["status"] == "done" and calls == ["a.txt"]
    assert fp.window_active("Claude (test)")
    with pytest.raises(LookupError):
        fp.approve_device(aid, appmode["app"].sign(aid, challenge="x"))
    assert calls == ["a.txt"]


def test_app_rejects_bad_signature_stale_challenge_and_foreign_origin(appmode):
    k = appmode["app"]
    tool = guard.guarded("phone_call", lambda number="1": "called", read_only=False)
    aid = tool()["approval_id"]
    ch = fp.device_options(aid)["challenge"]
    with pytest.raises(PermissionError, match="signature"):
        fp.approve_device(aid, k.sign(aid, ch, msg=b"something else"))
    with pytest.raises(PermissionError, match="challenge"):  # the failed attempt used the challenge up
        fp.approve_device(aid, k.sign(aid, ch))
    ch = fp.device_options(aid)["challenge"]
    foreign = f"{fp.DEVICE_MSG}\n{aid}\n{ch}\nhttps://evil.example".encode()
    with pytest.raises(PermissionError, match="signature"):
        fp.approve_device(aid, k.sign(aid, ch, msg=foreign))
    stranger = AppKey()
    stranger.kid = k.kid  # a key the server never saw, claiming the registered id
    with pytest.raises(PermissionError, match="signature"):
        fp.approve_device(aid, stranger.sign(aid))
    assert guard.load_approval(aid)["status"] == "pending"
    assert fp.approve_device(aid, k.sign(aid))["status"] == "done"


def test_app_cannot_approve_in_lost_mode(appmode):
    fp.set_lost(True)
    aid = guard.guarded("read_file", lambda path="x": "text", read_only=True)()["approval_id"]
    with pytest.raises(PermissionError, match="Lost-phone"):
        fp.device_options(aid)
    with pytest.raises(PermissionError, match="Lost-phone"):
        fp.approve_device(aid, appmode["app"].sign(aid, challenge="x"))


def test_push_opens_app_when_registered(appmode, monkeypatch):
    import rose.push
    sent = []
    monkeypatch.setattr(rose.push, "push", lambda title, message="", **k: sent.append(k))
    res = guard.guarded("read_file", lambda path="x": "text", read_only=True)()
    k = sent[-1]
    assert k["url"].startswith("rosevoice://approve?u=https%3A%2F%2Frose.example.test%2Frose%2Fapprove%3Fid%3D")
    assert k["actions"][0]["url"] == res["approval_url"]


def test_app_routes_and_page_link(appmode):
    from starlette.testclient import TestClient

    from rose import server
    client = TestClient(server.build_http_app("http://127.0.0.1:8765", 8765), base_url="http://127.0.0.1:8765")
    aid = guard.guarded("read_file", lambda path="x": "text", read_only=True)()["approval_id"]
    r = client.get(f"{guard.APPROVE_PATH}?id={aid}")
    assert "Approve in Rose app" in r.text and "intent://approve?u=" in r.text
    r = client.get(f"{fp.DEVICE_APPROVE_PATH}?id={aid}")
    assert r.status_code == 200 and r.json()["tool"] == "read_file"
    assert client.get(f"{fp.DEVICE_APPROVE_PATH}?id=nope").status_code == 404
    r = client.post(fp.DEVICE_APPROVE_PATH, json=appmode["app"].sign(aid))
    assert r.status_code == 200 and r.json()["status"] == "done"
    k = AppKey()
    tok = fp.new_registration("phone").split("t=", 1)[1]
    r = client.post(fp.DEVICE_REGISTER_PATH, json={"t": tok, "passphrase": appmode["passphrase"],
                                                   "public_key": k.spki, "device": "Pixel"})
    assert r.status_code == 200 and r.json()["name"] == "app-Pixel"


# ---------------- Rose app security screen ----------------

def _manage_open(app):
    ch = fp.manage_challenge(app.kid)["challenge"]
    sig = app.key.sign(fp.manage_message(app.kid, ch), ec.ECDSA(hashes.SHA256()))
    return fp.manage_open({"key_id": app.kid, "challenge": ch, "signature": webauthn.b64u(sig)})["session"]


def test_manage_session_needs_valid_single_use_signature(appmode):
    app = appmode["app"]
    with pytest.raises(LookupError):
        fp.manage_challenge("unknown")
    ch = fp.manage_challenge(app.kid)["challenge"]
    other = fp.manage_message(app.kid, "wrong")
    bad = app.key.sign(other, ec.ECDSA(hashes.SHA256()))
    with pytest.raises(PermissionError):
        fp.manage_open({"key_id": app.kid, "challenge": ch, "signature": webauthn.b64u(bad)})
    good = app.key.sign(fp.manage_message(app.kid, ch), ec.ECDSA(hashes.SHA256()))
    with pytest.raises(PermissionError):  # challenge was consumed by the failed try
        fp.manage_open({"key_id": app.kid, "challenge": ch, "signature": webauthn.b64u(good)})
    tok = _manage_open(app)
    with pytest.raises(PermissionError):
        fp.manage_keys("not-a-session")
    keys = fp.manage_keys(tok)["keys"]
    assert {k["kind"] for k in keys} == {"passkey", "app"}
    assert [k["name"] for k in keys if k["this_device"]] == ["app-A142"]


def test_manage_remove_and_self_revoke_ends_session(appmode):
    app = appmode["app"]
    tok = _manage_open(app)
    passkey = next(k["name"] for k in fp.manage_keys(tok)["keys"] if k["kind"] == "passkey")
    assert fp.manage_remove(tok, passkey)["removed"] == 1
    with pytest.raises(LookupError):
        fp.manage_remove(tok, "nope")
    res = fp.manage_remove(tok, "app-A142")
    assert res["self"] and "laptop fallback" in res["warning"]
    with pytest.raises(PermissionError):
        fp.manage_keys(tok)


def test_manage_rekey_keeps_name_and_drops_old_key(appmode):
    app = appmode["app"]
    tok = _manage_open(app)
    new = AppKey()
    with pytest.raises(PermissionError):
        fp.manage_rekey(tok, "wrong passphrase", new.spki)
    res = fp.manage_rekey(tok, appmode["passphrase"], new.spki)
    assert res["name"] == "app-A142" and res["key_id"] != app.kid
    with pytest.raises(PermissionError):  # session belonged to the old key
        fp.manage_keys(tok)
    with pytest.raises(LookupError):
        fp.manage_challenge(app.kid)
    new.kid = res["key_id"]
    assert fp.manage_keys(_manage_open(new))["keys"]


def test_manage_passphrase_change(appmode):
    from rose import auth
    tok = _manage_open(appmode["app"])
    with pytest.raises(PermissionError):
        fp.manage_passphrase(tok, "wrong passphrase", "a brand new passphrase")
    with pytest.raises(ValueError):
        fp.manage_passphrase(tok, appmode["passphrase"], "short")
    fp.manage_passphrase(tok, appmode["passphrase"], "a brand new passphrase")
    assert auth.check_passphrase("a brand new passphrase") and not auth.check_passphrase(appmode["passphrase"])


def test_manage_routes(appmode):
    from starlette.testclient import TestClient

    from rose import server
    app = appmode["app"]
    client = TestClient(server.build_http_app("http://127.0.0.1:8765", 8765), base_url="http://127.0.0.1:8765")
    assert client.get(f"{fp.DEVICE_CHALLENGE_PATH}?key_id=nope").status_code == 404
    ch = client.get(f"{fp.DEVICE_CHALLENGE_PATH}?key_id={app.kid}").json()["challenge"]
    sig = webauthn.b64u(app.key.sign(fp.manage_message(app.kid, ch), ec.ECDSA(hashes.SHA256())))
    r = client.post(fp.DEVICE_SESSION_PATH, json={"key_id": app.kid, "challenge": ch, "signature": sig})
    assert r.status_code == 200
    h = {"X-Rose-Session": r.json()["session"]}
    assert client.get(fp.DEVICE_KEYS_PATH).status_code == 401
    assert len(client.get(fp.DEVICE_KEYS_PATH, headers=h).json()["keys"]) == 2
    r = client.post(fp.DEVICE_PASSPHRASE_PATH, headers=h, json={"current": appmode["passphrase"], "new": "x"})
    assert r.status_code == 400
    r = client.post(fp.DEVICE_REMOVE_PATH, headers=h, json={"name": "nope"})
    assert r.status_code == 404
