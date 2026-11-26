"""Fingerprint approvals: every remote ROSE tool call needs a passkey (WebAuthn, user verification) on the phone.

Opt-in with `approval_mode = "fingerprint"` (`rose fingerprint enable`, which needs a registered passkey).
The default "passphrase" mode is untouched.

How a remote call is decided (guard.guarded calls gate()):

1. Lost-phone mode armed (`rose fingerprint lost on`): every call parks and only the laptop fallback
   (passphrase + a physical key press on the laptop) approves it.
2. Phone locked (ADB keyguard / screen asleep) -> the call is refused at once.
3. A short window (fingerprint_window_minutes, 1-10, default 5) opened by the last fingerprint lets that
   same caller run non-"fresh" tools without asking, only while the phone is known to be unlocked. It ends
   at once when the phone locks. Fresh tools (calls, messages, camera, mic) always need a new fingerprint.
4. Live frames (webcam_live_frame / screen_frame) run while the matching stream start was approved for
   this caller, until the phone locks or the stream stops.
5. Anything else parks: the phone gets a push that opens the approval page; "Approve with fingerprint"
   asks the phone's passkey (userVerification=required) and ROSE checks the signature against the key
   registered with `rose fingerprint register`.

Every decision is logged; every executed call is pushed to the phone (batched, never live frames).
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from rose import common, physkey, webauthn
from rose import push as pushmod
from rose.common import ToolError, _log
from rose.config import ROSE_HOME, get_public_url, load_config

PASSKEY_PATH = ROSE_HOME / "auth" / "passkeys.json"
LOCK_STATE = ROSE_HOME / "phone_lock.json"
WINDOWS = ROSE_HOME / "fp_windows.json"
STREAMS = ROSE_HOME / "fp_streams.json"
LOST_FLAG = ROSE_HOME / "FP_LOST"

WEBAUTHN_PATH = "/rose/approve/webauthn"
FALLBACK_PATH = "/rose/approve/fallback"
REGISTER_PATH = "/rose/passkey/register"
DEVICE_REGISTER_PATH = "/rose/device/register"
DEVICE_APPROVE_PATH = "/rose/approve/device"
# Rose app security screen: one fingerprint opens a short management session (list/revoke keys, re-key, passphrase).
DEVICE_CHALLENGE_PATH = "/rose/device/challenge"
DEVICE_SESSION_PATH = "/rose/device/session"
DEVICE_KEYS_PATH = "/rose/device/keys"
DEVICE_REMOVE_PATH = "/rose/device/keys/remove"
DEVICE_REKEY_PATH = "/rose/device/rekey"
DEVICE_PASSPHRASE_PATH = "/rose/device/passphrase"
MANAGE_MSG = "ROSE-MANAGE-v1"
MANAGE_CHALLENGE_TTL = 120
MANAGE_SESSION_TTL = 300
APP_SCHEME = "rosevoice"  # the Rose Voice phone app (dev.rose.voice) opens rosevoice://approve and ://register
APP_PACKAGE = "dev.rose.voice"
DEVICE_MSG = "ROSE-APPROVE-v1"
TEST_TOOL = "fingerprint_test"

LOCK_MAX_AGE = 5.0  # seconds before the guard re-checks the phone itself
LOCK_PROBE_TIMEOUT = 3.0
LOCK_EVERY = 2.0  # rose-watch polling interval
REG_TTL = 600
FALLBACK_SECONDS = 60
STREAM_TICKET_MAX = 30 * 60  # a live view approved once can't outlive this (or a phone lock, or *_live_stop)
NOTIFY_COALESCE = 10.0

DEFAULT_FRESH = ["phone_call", "phone_message", "phone_call_control", "webcam_capture", "webcam_live_stream",
                 "phone_photo", "phone_mic_record", "mic_live_start"]
# Never gated: reading an approval's status, and switching a live feed OFF (only ever reduces access).
ALWAYS_ALLOWED = {"check_approval", "webcam_live_stop", "screen_live_stop", "mic_live_stop"}
STREAM_OF_FRAME = {"webcam_live_frame": "webcam_live_stream", "screen_frame": "screen_live_stream"}
STREAM_OF_STOP = {"webcam_live_stop": "webcam_live_stream", "screen_live_stop": "screen_live_stream"}
LIVE_FRAMES = set(STREAM_OF_FRAME)

_file_lock = threading.RLock()
_probe_mutex = threading.Lock()
_claim_lock = threading.Lock()


# ---------------- small file helpers ----------------

def _read(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write(path: Path, data, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if private:
        os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2 if private else None)
    os.replace(tmp, path)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _event(decision: str, tool: str = "", who: str = "", **extra) -> None:
    _log({"event": "fingerprint", "decision": decision, "tool": tool, "caller": who, **extra})


# ---------------- config ----------------

def mode() -> str:
    return load_config().get("approval_mode", "passphrase")


def enabled() -> bool:
    return mode() == "fingerprint"


def window_minutes() -> int:
    try:
        return min(10, max(1, int(load_config().get("fingerprint_window_minutes", 5))))
    except (TypeError, ValueError):
        return 5


def fresh_tools() -> set[str]:
    v = load_config().get("fingerprint_fresh_tools")
    return set(v) if isinstance(v, list) else set(DEFAULT_FRESH)


def applies_to(who: str, is_remote: bool) -> bool:
    """Fingerprint rules apply to this caller (remote, or local when fingerprint_gate_local, minus exemptions)."""
    cfg = load_config()
    if who in (cfg.get("fingerprint_exempt_clients") or []):
        return False
    return is_remote or bool(cfg.get("fingerprint_gate_local", False))


def lost_armed() -> bool:
    return LOST_FLAG.exists()


def public_base() -> str:
    return (get_public_url() or f"http://127.0.0.1:{load_config()['http_port']}").rstrip("/")


def rp() -> tuple[str, str]:
    """(RP ID, origin) from the public URL: RP ID = its hostname, origin = scheme://host[:port]."""
    u = urlparse(public_base())
    return (u.hostname or ""), f"{u.scheme}://{u.netloc}"


# ---------------- passkeys ----------------

def _passkeys() -> dict:
    s = _read(PASSKEY_PATH, {})
    s.setdefault("credentials", {})
    s.setdefault("reg_tokens", {})
    s.setdefault("device_keys", {})
    return s


def _save_passkeys(s: dict) -> None:
    _write(PASSKEY_PATH, s, private=True)


def _user_id(s: dict) -> str:
    if not s.get("user_id"):
        s["user_id"] = webauthn.b64u(secrets.token_bytes(16))
    return s["user_id"]


def list_passkeys() -> list[dict]:
    return [{"name": c["name"], "created": c.get("created"), "last_used": c.get("last_used"),
             "alg": c.get("alg"), "sign_count": c.get("sign_count", 0)} for c in _passkeys()["credentials"].values()]


def list_device_keys() -> list[dict]:
    return [{"name": k["name"], "created": k.get("created"), "last_used": k.get("last_used"), "alg": "app-es256",
             "sign_count": 0} for k in _passkeys()["device_keys"].values()]


def has_app_key() -> bool:
    return bool(_passkeys()["device_keys"])


def remove_passkey(name: str) -> int:
    """Remove passkeys and Rose-app fingerprint keys with this name."""
    with _file_lock:
        s = _passkeys()
        before = len(s["credentials"]) + len(s["device_keys"])
        s["credentials"] = {k: v for k, v in s["credentials"].items() if v.get("name") != name}
        s["device_keys"] = {k: v for k, v in s["device_keys"].items() if v.get("name") != name}
        _save_passkeys(s)
    _event("passkey_removed", who="local", name=name)
    return before - len(s["credentials"]) - len(s["device_keys"])


def app_link(kind: str, **params: str) -> str:
    """rosevoice://<kind>?... for the Rose Voice app (ntfy actions and plain links open it directly)."""
    return f"{APP_SCHEME}://{kind}?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items())


def app_intent(kind: str, **params: str) -> str:
    """The same link as a Chrome intent:// URL (a web page can only open an app this way)."""
    q = "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items())
    return f"intent://{kind}?{q}#Intent;scheme={APP_SCHEME};package={APP_PACKAGE};end"


def new_registration(name: str = "phone") -> str:
    """One-time registration token (10 min, stored hashed); returns the URL to open on the phone."""
    tok = secrets.token_urlsafe(24)
    with _file_lock:
        s = _passkeys()
        now = time.time()
        s["reg_tokens"] = {k: v for k, v in s["reg_tokens"].items() if v["expires"] > now}
        s["reg_tokens"][_hash(tok)] = {"expires": now + REG_TTL, "name": name[:40] or "phone"}
        _save_passkeys(s)
    _event("registration_started", who="local", name=name)
    return f"{public_base()}{REGISTER_PATH}?t={tok}"


def _reg_entry(s: dict, tok: str) -> dict | None:
    e = s["reg_tokens"].get(_hash(tok or ""))
    return e if e and e["expires"] > time.time() else None


def registration_valid(tok: str) -> bool:
    return _reg_entry(_passkeys(), tok) is not None


def _lockout_check() -> None:
    from rose import guard
    now = time.time()
    guard._approve_failures[:] = [t for t in guard._approve_failures if now - t < 900]
    if len(guard._approve_failures) >= 5:
        raise PermissionError("Too many failed attempts. Try again in 15 minutes.")


def _fail() -> None:
    from rose import guard
    guard._approve_failures.append(time.time())


def registration_options(tok: str, passphrase: str) -> dict:
    """Passphrase checked -> PublicKeyCredentialCreationOptions (JSON, base64url fields)."""
    from rose import auth
    _lockout_check()
    with _file_lock:
        s = _passkeys()
        e = _reg_entry(s, tok)
        if not e:
            raise LookupError("This registration link expired. Run `rose fingerprint register` again.")
        if not auth.check_passphrase(passphrase):
            _fail()
            _event("registration_wrong_passphrase", who="phone")
            raise PermissionError("Wrong passphrase.")
        e["challenge"] = webauthn.b64u(secrets.token_bytes(32))
        uid = _user_id(s)
        _save_passkeys(s)
    rp_id, _ = rp()
    return {
        "challenge": e["challenge"],
        "rp": {"id": rp_id, "name": "ROSE"},
        "user": {"id": uid, "name": "rose-owner", "displayName": "ROSE owner"},
        "pubKeyCredParams": [{"type": "public-key", "alg": a} for a in (-7, -8, -257)],
        "authenticatorSelection": {"authenticatorAttachment": "platform", "userVerification": "required",
                                   "residentKey": "preferred"},
        "attestation": "none",
        "timeout": 120000,
        "excludeCredentials": [{"type": "public-key", "id": cid} for cid in s["credentials"]],
    }


def registration_finish(tok: str, data: dict) -> dict:
    """Verify the attestation and store the passkey; the token is used up on success."""
    _lockout_check()
    rp_id, origin = rp()
    with _file_lock:
        s = _passkeys()
        e = _reg_entry(s, tok)
        if not e or not e.get("challenge"):
            raise LookupError("This registration link expired or wasn't started. Run `rose fingerprint register` again.")
        challenge = e.pop("challenge")  # single use, also on failure
        _save_passkeys(s)
        try:
            res = webauthn.verify_registration(webauthn.b64u_dec(data.get("clientDataJSON", "")),
                                               webauthn.b64u_dec(data.get("attestationObject", "")),
                                               challenge, origin, rp_id)
        except webauthn.WebAuthnError as err:
            _fail()
            _event("registration_failed", who="phone", reason=str(err))
            raise PermissionError(f"Passkey check failed: {err}") from None
        cid = webauthn.b64u(res["cred_id"])
        if cid in s["credentials"]:
            raise PermissionError("This passkey is already registered.")
        name = e["name"]
        taken = {c["name"] for c in s["credentials"].values()}
        n = 2
        while name in taken:
            name, n = f"{e['name']}-{n}", n + 1
        s["credentials"][cid] = {"name": name, "cose": webauthn.b64u(res["cose_key"]), "alg": res["alg"],
                                 "sign_count": res["sign_count"], "created": int(time.time()), "last_used": None,
                                 "transports": [t for t in (data.get("transports") or []) if isinstance(t, str)][:6]}
        del s["reg_tokens"][_hash(tok)]
        _save_passkeys(s)
    _event("passkey_registered", who="phone", name=name, alg=res["alg"])
    _notify_push("🔑 ROSE passkey registered", f"New passkey '{name}' added to ROSE. If this wasn't you: "
                 "rose fingerprint remove " + name, "high", "key")
    return {"name": name, "alg": res["alg"]}


def _load_p256(spki_b64u: str):
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import load_der_public_key
    try:
        der = webauthn.b64u_dec(spki_b64u)
        if len(der) > 512:
            raise ValueError
        pub = load_der_public_key(der)
    except (ValueError, TypeError, webauthn.WebAuthnError):
        raise PermissionError("Bad public key.") from None
    if not isinstance(pub, ec.EllipticCurvePublicKey) or pub.curve.name != "secp256r1":
        raise PermissionError("The app key must be EC P-256.")
    return der, pub


def device_register(tok: str, passphrase: str, public_key: str, device: str = "") -> dict:
    """The Rose Voice app registers its fingerprint-locked Keystore key (registration link + passphrase)."""
    from rose import auth
    _lockout_check()
    der, _ = _load_p256(public_key)
    with _file_lock:
        s = _passkeys()
        e = _reg_entry(s, tok)
        if not e:
            raise LookupError("This registration link expired or was used. Run `rose fingerprint register-app` again.")
        if not auth.check_passphrase(passphrase):
            _fail()
            _event("registration_wrong_passphrase", who="rose-app")
            raise PermissionError("Wrong passphrase.")
        kid = webauthn.b64u(hashlib.sha256(der).digest())[:22]
        base_name = (e["name"] if e["name"] != "phone" else f"app-{device}" if device else "app")[:40]
        taken = {c["name"] for c in s["credentials"].values()} | {k["name"] for k in s["device_keys"].values()
                                                                    if k is not s["device_keys"].get(kid)}
        name, n = base_name, 2
        while name in taken:
            name, n = f"{base_name}-{n}", n + 1
        s["device_keys"][kid] = {"name": name, "spki": webauthn.b64u(der), "created": int(time.time()),
                                 "last_used": None, "device": device[:60]}
        del s["reg_tokens"][_hash(tok)]
        _save_passkeys(s)
    _event("app_key_registered", who="rose-app", name=name)
    _notify_push("🔑 ROSE app fingerprint registered", f"The Rose app key '{name}' can now approve ROSE actions. "
                 "If this wasn't you: rose fingerprint remove " + name, "high", "key")
    return {"name": name, "key_id": kid}


# ---------------- phone lock state ----------------

def _probe_lock() -> str:
    """'locked' / 'unlocked' / 'unknown' straight from the phone over ADB (short timeout)."""
    import re

    from rose.tools import phone
    try:
        out = str(phone._adb("shell", "dumpsys window | grep -E 'mDreamingLockscreen|isKeyguardShowing|"
                             "mShowingLockscreen|KeyguardShowing' ; dumpsys power | grep -m1 mWakefulness=",
                             timeout=LOCK_PROBE_TIMEOUT, _retry=False))
    except Exception:
        return "unknown"
    if "mWakefulness=Asleep" in out or "mWakefulness=Dozing" in out:
        return "locked"
    if re.search(r"(KeyguardShowing|ShowingLockscreen|DreamingLockscreen)\W*[=:]\s*true", out, re.I):
        return "locked"
    if "mWakefulness=Awake" in out:  # positively awake and no keyguard: only then "unlocked"
        return "unlocked"
    return "unknown"


def set_lock_state(state: str, source: str = "guard") -> str:
    """Record the phone's lock state; leaving 'unlocked' ends every window and live-view ticket."""
    with _file_lock:
        prev = _read(LOCK_STATE, {}).get("state")
        _write(LOCK_STATE, {"state": state, "ts": time.time(), "source": source})
    if prev == "unlocked" and state != "unlocked":
        ended = end_windows(f"phone {state}")
        _event("phone_" + state, who=source, windows_ended=ended)
        if ended:
            _notify_push("ROSE window closed: phone locked" if state == "locked" else "ROSE window closed: phone unreachable",
                         "The next ROSE action needs your fingerprint again.", "low", "lock")
    return state


def lock_state() -> str:
    """Current lock state; re-checked over ADB when the stored one is older than LOCK_MAX_AGE seconds."""
    st = _read(LOCK_STATE, {})
    if st.get("state") in ("locked", "unlocked", "unknown") and time.time() - float(st.get("ts", 0)) <= LOCK_MAX_AGE:
        return st["state"]
    if not _probe_mutex.acquire(timeout=LOCK_PROBE_TIMEOUT + 2):
        return "unknown"
    try:
        st = _read(LOCK_STATE, {})  # someone else may have just refreshed it
        if st.get("state") and time.time() - float(st.get("ts", 0)) <= LOCK_MAX_AGE:
            return st["state"]
        return set_lock_state(_probe_lock(), "guard")
    finally:
        _probe_mutex.release()


def lock_loop() -> None:
    """rose-watch thread: poll the phone every 2 s while fingerprint mode is on."""
    while True:
        try:
            if enabled() and load_config().get("watch_enabled", True):
                set_lock_state(_probe_lock(), "watch")
        except Exception as e:
            _log({"event": "watch_error", "watcher": "fp-lock", "error": f"{type(e).__name__}: {e}"})
        time.sleep(LOCK_EVERY)


# ---------------- windows and live-view tickets ----------------

def window_active(who: str) -> bool:
    w = _read(WINDOWS, {}).get(who)
    return bool(w) and float(w.get("until", 0)) > time.time()


def open_window(who: str, approval_id: str) -> None:
    with _file_lock:
        w = {k: v for k, v in _read(WINDOWS, {}).items() if float(v.get("until", 0)) > time.time()}
        w[who] = {"until": time.time() + 60 * window_minutes(), "approval_id": approval_id}
        _write(WINDOWS, w)
    _event("window_opened", who=who, minutes=window_minutes())


def end_windows(reason: str = "") -> int:
    """End every fingerprint window and live-view ticket; returns how many were active."""
    with _file_lock:
        now = time.time()
        n = sum(1 for v in _read(WINDOWS, {}).values() if float(v.get("until", 0)) > now)
        n += sum(1 for c in _read(STREAMS, {}).values() for u in c.values() if float(u) > now)
        WINDOWS.unlink(missing_ok=True)
        STREAMS.unlink(missing_ok=True)
    if n:
        _event("windows_ended", reason=reason, count=n)
    return n


def _ticket_valid(who: str, stream: str) -> bool:
    return float(_read(STREAMS, {}).get(who, {}).get(stream, 0)) > time.time()


def _set_ticket(who: str, stream: str) -> None:
    with _file_lock:
        s = _read(STREAMS, {})
        s.setdefault(who, {})[stream] = time.time() + STREAM_TICKET_MAX
        _write(STREAMS, s)


def _end_ticket(stream: str) -> None:
    with _file_lock:
        s = _read(STREAMS, {})
        for c in s.values():
            c.pop(stream, None)
        _write(STREAMS, s)


def status() -> dict:
    now = time.time()
    return {"mode": mode(), "passkeys": [p["name"] for p in list_passkeys() + list_device_keys()],
            "phone": _read(LOCK_STATE, {}).get("state", "unknown"), "lost_mode": lost_armed(),
            "window_minutes": window_minutes(),
            "windows": {k: int(v["until"] - now) for k, v in _read(WINDOWS, {}).items() if v.get("until", 0) > now}}


# ---------------- per-call notifications ----------------

_note_lock = threading.Lock()
_notes: list[tuple[str, str]] = []
_note_timer: threading.Timer | None = None


def _notify_push(title: str, message: str, priority: str = "low", tags: str = "") -> None:
    try:
        pushmod.push(title, message, priority=priority, tags=tags, desktop=False)
    except Exception:
        pass


def note_call(who: str, tool: str) -> None:
    """Queue a 'ROSE: <caller> ran <tool>' push; calls within NOTIFY_COALESCE seconds go out as one push."""
    global _note_timer
    if tool in LIVE_FRAMES or not load_config().get("notify_every_call", True):
        return
    with _note_lock:
        _notes.append((who, tool))
        if _note_timer is None:
            _note_timer = threading.Timer(NOTIFY_COALESCE, flush_notifications)
            _note_timer.daemon = True
            _note_timer.start()


def flush_notifications() -> None:
    global _note_timer
    with _note_lock:
        batch, _notes[:] = list(_notes), []
        if _note_timer is not None:
            _note_timer.cancel()
        _note_timer = None
    if not batch:
        return
    if len(batch) == 1:
        _notify_push(f"ROSE: {batch[0][0]} ran {batch[0][1]}", "Not you? Run `rose lockdown on` on the laptop.")
        return
    callers = sorted({w for w, _ in batch})
    tools = list(dict.fromkeys(t for _, t in batch))
    shown = ", ".join(tools[:8]) + (f" +{len(tools) - 8} more" if len(tools) > 8 else "")
    _notify_push(f"ROSE: {len(batch)} calls by {', '.join(callers)[:60]}", f"{shown}. Not you? `rose lockdown on`.")


# ---------------- the gate ----------------

def _new_approval(name: str, args: dict, who: str, fresh: bool, lost: bool = False, test: bool = False) -> dict:
    from rose import guard
    guard.APPROVAL_DIR.mkdir(parents=True, exist_ok=True)
    aid = secrets.token_urlsafe(18)
    summary = guard._describe(name, args)
    mins = 0 if (fresh or lost or test) else window_minutes()
    if mins:
        summary += (f" (approving also lets {who} run other non-sensitive ROSE tools for {mins} minutes, "
                    "until your phone locks)")
    rec = {"id": aid, "tool": name, "args": args, "caller": who, "created": time.time(), "status": "pending",
           "summary": summary, "grant_minutes": 0, "mode": "fingerprint", "fresh": bool(fresh),
           "window_minutes": mins, "lost": bool(lost), "test": bool(test),
           "challenge": webauthn.b64u(secrets.token_bytes(32))}
    guard._save(rec)
    _event("approval_requested", name, who, lost=lost, fresh=fresh)
    url = f"{public_base()}{guard.APPROVE_PATH}?id={aid}"
    try:  # only the tool name and caller; the details stay behind the link
        app = not lost and has_app_key()  # tap -> the Rose app's fingerprint prompt; the page stays one button away
        pushmod.push("🔐 ROSE fingerprint needed" if not lost else "🔐 ROSE approval (lost-phone mode)",
                     f"{who} wants to run {name}. " + ("Tap and touch the sensor." if app else "Tap to review."),
                     url=app_link("approve", u=url) if app else url, priority="high", tags="lock", desktop=True,
                     actions=[{"action": "view", "label": "Open page", "url": url, "clear": True}] if app else None)
    except Exception:
        pass
    return {
        "status": "approval_required",
        "approval_id": aid,
        "approval_url": url,
        "action": summary,
        "next": ("Nothing has happened yet. ROSE sent an approval request to the user's phone. Tell them what "
                 "it will do; they approve it with their fingerprint on the phone (or, if the phone is dead, the "
                 "laptop fallback) within 10 minutes. Then call check_approval(approval_id) for the result. "
                 "Never ask the user for their passphrase in chat."),
    }


def _pending_for(who: str, name: str) -> dict | None:
    """An existing pending fingerprint approval for this caller+tool (so polled tools don't spam the phone)."""
    from rose import guard
    for p in sorted(guard.APPROVAL_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
        rec = guard.load_approval(p.stem)
        if rec and rec["status"] == "pending" and rec.get("mode") == "fingerprint" and rec["caller"] == who \
                and rec["tool"] == name:
            return rec
    return None


def gate(name: str, bound: dict, who: str) -> dict | None:
    """Decide one gated call: None = run it now; a dict = parked (approval link); raises ToolError = denied."""
    if name in ALWAYS_ALLOWED:
        _event("allowed_always", name, who)
        return None
    if lost_armed():
        _event("lost_mode_park", name, who)
        return _new_approval(name, bound, who, fresh=True, lost=True)
    state = lock_state()
    if state == "locked":
        _event("denied_phone_locked", name, who)
        raise ToolError("The user's phone is locked, so ROSE refuses every action right now. Tell the user to "
                        "unlock their phone and ask again (or, if the phone is lost, use `rose fingerprint lost on` "
                        "on the laptop).")
    if name in STREAM_OF_FRAME:
        if state == "unlocked" and _ticket_valid(who, STREAM_OF_FRAME[name]):
            _event("allowed_stream", name, who)
            return None
        if name == "webcam_live_frame":
            raise ToolError("No approved webcam live view: call webcam_live_stream first (it needs the user's "
                            "fingerprint), then poll webcam_live_frame.")
    fresh = name in fresh_tools()
    if not fresh and state == "unlocked" and window_active(who):
        _event("allowed_window", name, who)
        return None
    if name in LIVE_FRAMES:  # polled many times a second: one pending approval at a time
        existing = _pending_for(who, name)
        if existing:
            from rose import guard
            return {"status": "approval_required", "approval_id": existing["id"],
                    "approval_url": f"{public_base()}{guard.APPROVE_PATH}?id={existing['id']}",
                    "action": existing["summary"], "next": "Waiting for the user's fingerprint on the phone."}
    return _new_approval(name, bound, who, fresh=fresh)


def after_call(name: str, who: str, ok: bool = True) -> None:
    """Bookkeeping after a gated call ran: live-view tickets and the phone notification."""
    if ok and name in STREAM_OF_FRAME.values():
        _set_ticket(who, name)
    if name in STREAM_OF_STOP:
        _end_ticket(STREAM_OF_STOP[name])
    note_call(who, name)


# ---------------- approving ----------------

def _claim(aid: str) -> dict:
    """Atomically move a pending fingerprint approval to 'running' (so it runs exactly once)."""
    from rose import guard
    with _claim_lock:
        rec = guard.load_approval(aid)
        if not rec or rec["status"] != "pending":
            raise LookupError(f"This approval is {rec['status'] if rec else 'unknown'}.")
        if guard.lockdown():
            raise PermissionError("ROSE is in lockdown; turn it off first (rose lockdown off).")
        rec["status"] = "running"
        guard._save(rec)
        return rec


def _execute(rec: dict, how: str) -> dict:
    from rose import guard
    if rec.get("test"):
        rec["status"], rec["result"], rec["finished"] = "done", f"Fingerprint round trip OK ({how}).", time.time()
        guard._save(rec)
        _event("test_ok", TEST_TOOL, rec["caller"], how=how)
        return rec
    rec = guard._run_parked(rec)
    after_call(rec["tool"], rec["caller"], ok=rec["status"] == "done")
    return rec


def assertion_options(rec: dict) -> dict:
    rp_id, _ = rp()
    creds = _passkeys()["credentials"]
    return {"challenge": rec["challenge"], "rpId": rp_id, "userVerification": "required", "timeout": 60000,
            "allowCredentials": [{"type": "public-key", "id": cid, **({"transports": c["transports"]} if c.get("transports") else {})}
                                 for cid, c in creds.items()]}


def approve_webauthn(aid: str, data: dict) -> dict:
    """Verify a passkey assertion for a parked fingerprint approval, then run the call exactly once."""
    from rose import guard
    _lockout_check()
    rec = guard.load_approval(aid)
    if not rec or rec["status"] != "pending":
        raise LookupError(f"This approval is {rec['status'] if rec else 'unknown'}.")
    if rec.get("mode") != "fingerprint":
        raise PermissionError("This approval uses the passphrase, not a fingerprint.")
    if rec.get("lost") or lost_armed():
        raise PermissionError("Lost-phone mode is on: approve with the laptop fallback below.")
    rp_id, origin = rp()
    with _claim_lock:  # use up this challenge now, whatever happens (single use)
        rec = guard.load_approval(aid)
        if not rec or rec["status"] != "pending":
            raise LookupError(f"This approval is {rec['status'] if rec else 'unknown'}.")
        challenge = rec["challenge"]
        rec["challenge"] = webauthn.b64u(secrets.token_bytes(32))
        guard._save(rec)
    with _file_lock:
        s = _passkeys()
        cid = data.get("credentialId", "")
        cred = s["credentials"].get(cid) if isinstance(cid, str) else None
        try:
            if not cred:
                raise webauthn.WebAuthnError("unknown passkey")
            count = webauthn.verify_assertion(
                webauthn.b64u_dec(data.get("clientDataJSON", "")), webauthn.b64u_dec(data.get("authenticatorData", "")),
                webauthn.b64u_dec(data.get("signature", "")), challenge, origin, rp_id,
                webauthn.b64u_dec(cred["cose"]), int(cred.get("sign_count", 0)))
        except webauthn.WebAuthnError as err:
            _fail()
            _event("fingerprint_failed", rec["tool"], rec["caller"], reason=str(err))
            _notify_push("⚠️ ROSE: fingerprint check failed", f"An approval for {rec['tool']} failed verification.",
                         "high", "warning")
            raise PermissionError(f"Fingerprint check failed: {err}") from None
        cred["sign_count"], cred["last_used"] = count, int(time.time())
        _save_passkeys(s)
    return _approved(aid, cred["name"], "fingerprint")


def _approved(aid: str, key_name: str, how: str) -> dict:
    """A fingerprint check passed: claim the approval, open the window, run the call once."""
    rec = _claim(aid)
    _event("fingerprint_ok", rec["tool"], rec["caller"], passkey=key_name, how=how)
    if rec.get("window_minutes") and not rec.get("fresh") and not rec.get("test"):
        open_window(rec["caller"], aid)
    return _execute(rec, how)


def _pending_fp(aid: str) -> dict:
    from rose import guard
    rec = guard.load_approval(aid)
    if not rec or rec["status"] != "pending":
        raise LookupError(f"This approval is {rec['status'] if rec else 'unknown'}.")
    if rec.get("mode") != "fingerprint":
        raise PermissionError("This approval uses the passphrase, not a fingerprint.")
    if rec.get("lost") or lost_armed():
        raise PermissionError("Lost-phone mode is on: approve with the laptop fallback on the approval page.")
    return rec


def device_message(aid: str, challenge: str) -> bytes:
    """What the Rose app signs: bound to this approval, its current challenge and this ROSE's origin."""
    return f"{DEVICE_MSG}\n{aid}\n{challenge}\n{rp()[1]}".encode()


def device_options(aid: str) -> dict:
    """What the Rose app shows in its fingerprint prompt, plus the challenge to sign."""
    from rose import guard
    rec = _pending_fp(aid)
    return {"id": rec["id"], "tool": rec["tool"], "caller": rec["caller"], "summary": rec["summary"],
            "challenge": rec["challenge"], "origin": rp()[1], "fresh": bool(rec.get("fresh")),
            "window_minutes": int(rec.get("window_minutes") or 0),
            "expires_in": max(0, int(rec["created"] + guard.APPROVAL_TTL - time.time()))}


def approve_device(aid: str, data: dict) -> dict:
    """Verify the Rose app's fingerprint-unlocked Keystore signature, then run the call exactly once."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from rose import guard
    _lockout_check()
    _pending_fp(aid)
    with _claim_lock:  # the challenge is single use, whatever happens
        rec = guard.load_approval(aid)
        if not rec or rec["status"] != "pending":
            raise LookupError(f"This approval is {rec['status'] if rec else 'unknown'}.")
        challenge = rec["challenge"]
        rec["challenge"] = webauthn.b64u(secrets.token_bytes(32))
        guard._save(rec)
    with _file_lock:
        s = _passkeys()
        kid = data.get("key_id", "")
        key = s["device_keys"].get(kid) if isinstance(kid, str) else None
        try:
            if not key:
                raise webauthn.WebAuthnError("unknown app key (register the app again)")
            got = data.get("challenge", "")
            if not isinstance(got, str) or not secrets.compare_digest(got, challenge):
                raise webauthn.WebAuthnError("challenge mismatch (stale prompt; try again)")
            _, pub = _load_p256(key["spki"])
            try:
                pub.verify(webauthn.b64u_dec(data.get("signature", "")), device_message(aid, challenge),
                           ec.ECDSA(hashes.SHA256()))
            except InvalidSignature:
                raise webauthn.WebAuthnError("signature does not verify") from None
        except (webauthn.WebAuthnError, PermissionError) as err:
            _fail()
            _event("fingerprint_failed", rec["tool"], rec["caller"], reason=str(err), how="app")
            _notify_push("⚠️ ROSE: fingerprint check failed", f"An app approval for {rec['tool']} failed verification.",
                         "high", "warning")
            raise PermissionError(f"Fingerprint check failed: {err}") from None
        key["last_used"] = int(time.time())
        _save_passkeys(s)
    return _approved(aid, key["name"], "Rose app fingerprint")


# ---------------- Rose app security screen (management session) ----------------
#
# The app asks for a challenge for its key id, signs ROSE-MANAGE-v1\n<key_id>\n<challenge>\n<origin> with its
# fingerprint-locked Keystore key, and gets a session token valid MANAGE_SESSION_TTL seconds. The session can list
# and revoke keys; re-keying and changing the passphrase also need the current ROSE passphrase. Everything is
# logged and pushed to the phone. Challenges and sessions live in memory only (a restart ends them).

_manage_lock = threading.Lock()
_manage_challenges: dict[str, tuple[str, float]] = {}  # key_id -> (challenge, expires)
_manage_sessions: dict[str, dict] = {}  # sha256(token) -> {"kid", "until"}


def manage_message(kid: str, challenge: str) -> bytes:
    return f"{MANAGE_MSG}\n{kid}\n{challenge}\n{rp()[1]}".encode()


def manage_challenge(kid: str) -> dict:
    """A single-use challenge for a registered app key (unknown ids get nothing, so memory stays bounded)."""
    if not isinstance(kid, str) or kid not in _passkeys()["device_keys"]:
        raise LookupError("This app isn't registered with ROSE (register it again).")
    ch = webauthn.b64u(secrets.token_bytes(32))
    with _manage_lock:
        _manage_challenges[kid] = (ch, time.time() + MANAGE_CHALLENGE_TTL)
    return {"challenge": ch, "origin": rp()[1], "expires_in": MANAGE_CHALLENGE_TTL}


def manage_open(data: dict) -> dict:
    """Verify the fingerprint signature over the challenge; returns a short-lived session token."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    _lockout_check()
    kid = data.get("key_id", "")
    kid = kid if isinstance(kid, str) else ""
    with _manage_lock:
        ch, until = _manage_challenges.pop(kid, ("", 0.0))  # single use, whatever happens
    try:
        key = _passkeys()["device_keys"].get(kid)
        if not key:
            raise webauthn.WebAuthnError("unknown app key (register the app again)")
        got = data.get("challenge", "")
        if not ch or until < time.time() or not isinstance(got, str) or not secrets.compare_digest(got, ch):
            raise webauthn.WebAuthnError("challenge expired or mismatched (try again)")
        _, pub = _load_p256(key["spki"])
        try:
            pub.verify(webauthn.b64u_dec(data.get("signature", "")), manage_message(kid, ch),
                       ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            raise webauthn.WebAuthnError("signature does not verify") from None
    except (webauthn.WebAuthnError, PermissionError, ValueError) as err:
        _fail()
        _event("manage_failed", who="rose-app", reason=str(err))
        _notify_push("⚠️ ROSE: security screen check failed", "A Rose app fingerprint check failed.", "high", "warning")
        raise PermissionError(f"Fingerprint check failed: {err}") from None
    tok = secrets.token_urlsafe(32)
    now = time.time()
    with _manage_lock:
        for h in [h for h, v in _manage_sessions.items() if v["until"] < now]:
            del _manage_sessions[h]
        _manage_sessions[_hash(tok)] = {"kid": kid, "until": now + MANAGE_SESSION_TTL}
    _event("manage_opened", who="rose-app", name=key["name"])
    return {"session": tok, "name": key["name"], "expires_in": MANAGE_SESSION_TTL}


def _manage_session(tok: str) -> str:
    """The key id behind a live session token, or PermissionError."""
    with _manage_lock:
        s = _manage_sessions.get(_hash(tok or ""))
        if not s or s["until"] < time.time():
            raise PermissionError("Session expired. Unlock again with your fingerprint.")
        kid = s["kid"]
    if kid not in _passkeys()["device_keys"]:
        _end_manage_sessions(kid)
        raise PermissionError("This app's key was revoked.")
    return kid


def _end_manage_sessions(kid: str) -> None:
    with _manage_lock:
        for h in [h for h, v in _manage_sessions.items() if v["kid"] == kid]:
            del _manage_sessions[h]


def manage_keys(tok: str) -> dict:
    """Every key that can approve ROSE actions, plus the approval settings the app shows."""
    kid = _manage_session(tok)
    s = _passkeys()
    keys = [{"name": c["name"], "kind": "passkey", "created": c.get("created"), "last_used": c.get("last_used"),
             "this_device": False} for c in s["credentials"].values()]
    keys += [{"name": k["name"], "kind": "app", "device": k.get("device", ""), "created": k.get("created"),
              "last_used": k.get("last_used"), "this_device": i == kid} for i, k in s["device_keys"].items()]
    return {"keys": keys, "mode": mode(), "lost_mode": lost_armed(), "window_minutes": window_minutes(),
            "kinds": [{"kind": "app", "label": "Rose app fingerprint (Android Keystore, signs per use)"},
                      {"kind": "passkey", "label": "Passkey (WebAuthn, browser prompt)"}]}


def manage_remove(tok: str, name: str) -> dict:
    kid = _manage_session(tok)
    if not isinstance(name, str) or not name:
        raise LookupError("No key name given.")
    own = _passkeys()["device_keys"].get(kid, {}).get("name") == name
    n = remove_passkey(name)
    if not n:
        raise LookupError(f"No key named {name}.")
    if own:
        _end_manage_sessions(kid)
    _notify_push("🔑 ROSE key revoked", f"'{name}' can no longer approve ROSE actions (revoked from the Rose app).",
                 "high", "key")
    left = len(list_passkeys()) + len(list_device_keys())
    warn = "Fingerprint mode is on but no key is left: only the laptop fallback can approve." \
        if enabled() and not left else ""
    return {"removed": n, "self": own, "warning": warn}


def manage_rekey(tok: str, passphrase: str, public_key: str) -> dict:
    """Swap this app's key for a new one (keeps its name). Needs the session and the ROSE passphrase."""
    from rose import auth
    _lockout_check()
    kid = _manage_session(tok)
    der, _ = _load_p256(public_key)
    if not auth.check_passphrase(passphrase):
        _fail()
        _event("rekey_wrong_passphrase", who="rose-app")
        raise PermissionError("Wrong passphrase.")
    new_kid = webauthn.b64u(hashlib.sha256(der).digest())[:22]
    with _file_lock:
        s = _passkeys()
        old = s["device_keys"].pop(kid, None)
        if not old:
            raise PermissionError("This app's key was revoked.")
        s["device_keys"][new_kid] = {**old, "spki": webauthn.b64u(der), "created": int(time.time()), "last_used": None}
        _save_passkeys(s)
    _end_manage_sessions(kid)
    _event("app_key_rekeyed", who="rose-app", name=old["name"])
    _notify_push("🔑 ROSE app key changed", f"The Rose app key '{old['name']}' was replaced with a new one.", "high", "key")
    return {"name": old["name"], "key_id": new_kid}


def manage_passphrase(tok: str, current: str, new: str) -> dict:
    from rose import auth
    _lockout_check()
    _manage_session(tok)
    if not auth.check_passphrase(current):
        _fail()
        _event("passphrase_change_wrong", who="rose-app")
        raise PermissionError("Wrong current passphrase.")
    if new == current:
        raise ValueError("The new passphrase is the same as the old one.")
    auth.set_passphrase(new)
    _event("passphrase_changed", who="rose-app")
    _notify_push("🔐 ROSE passphrase changed", "Changed from the Rose app. If this wasn't you: rose auth set-passphrase "
                 "on the laptop.", "high", "key")
    return {"message": "Passphrase changed."}


# ---------------- laptop fallback (dead or lost phone) ----------------

_fallback_threads: dict[str, threading.Thread] = {}


def fallback_start(aid: str, passphrase: str, wait: bool = False) -> dict:
    """Passphrase checked -> ask for one physical key press on the laptop within 60 s; returns the record."""
    from rose import auth, guard
    _lockout_check()
    rec = guard.load_approval(aid)
    if not rec or rec["status"] != "pending" or rec.get("mode") != "fingerprint":
        raise LookupError(f"This approval is {rec['status'] if rec else 'unknown'}.")
    fb = rec.get("fallback") or {}
    if fb.get("until", 0) > time.time():
        return rec  # already waiting for the key
    if not auth.check_passphrase(passphrase):
        _fail()
        _event("fallback_wrong_passphrase", rec["tool"], rec["caller"])
        raise PermissionError("Wrong passphrase.")
    key = physkey.random_key()
    rec["fallback"] = {"key": key, "until": time.time() + FALLBACK_SECONDS}
    guard._save(rec)
    _event("fallback_started", rec["tool"], rec["caller"])
    common.notify_desktop("ROSE: press a key to approve",
                          f"Press {key} on THIS laptop's keyboard within {FALLBACK_SECONDS} s to approve "
                          f"{rec['tool']} for {rec['caller']}. Didn't ask? Do nothing.", urgency="critical")
    _notify_push("⚠️ ROSE laptop fallback used", f"Passphrase entered to approve {rec['tool']} without the phone. "
                 "Not you? Run `rose lockdown on`.", "high", "warning")
    t = threading.Thread(target=_fallback_wait, args=(aid, key), daemon=True)
    _fallback_threads[aid] = t
    t.start()
    if wait:
        t.join()
    return guard.load_approval(aid) or rec


def _fallback_wait(aid: str, key: str) -> None:
    from rose import guard
    try:
        ok = physkey.wait_for_key(key, FALLBACK_SECONDS)
        err = ""
    except Exception as e:
        ok, err = False, f"{type(e).__name__}: {e}"
    rec = guard.load_approval(aid)
    if not ok:
        if rec and rec["status"] == "pending":
            rec.pop("fallback", None)
            rec["fallback_error"] = err or "No key press in time."
            guard._save(rec)
        _event("fallback_timeout", rec["tool"] if rec else "", rec["caller"] if rec else "", error=err)
        return
    try:
        rec = _claim(aid)
    except (LookupError, PermissionError) as e:
        _event("fallback_refused", error=str(e))
        return
    _event("fallback_ok", rec["tool"], rec["caller"])
    _notify_push("ROSE laptop fallback approved", f"{rec['caller']} ran {rec['tool']} (passphrase + key press).",
                 "high", "warning")
    _execute(rec, "laptop fallback")  # one-shot: never opens a window


def local_physical_check(passphrase: str, prompt=print) -> bool:
    """CLI: passphrase + a physical key press (used to arm/disarm lost-phone mode)."""
    from rose import auth
    _lockout_check()
    if not auth.check_passphrase(passphrase):
        _fail()
        _event("lost_mode_wrong_passphrase", who="local")
        return False
    key = physkey.random_key()
    prompt(f"Press {key} on the laptop's built-in or USB keyboard within {FALLBACK_SECONDS} s...")
    try:
        return physkey.wait_for_key(key, FALLBACK_SECONDS)
    except RuntimeError as e:
        prompt(str(e))
        return False


def set_lost(on: bool) -> None:
    if on:
        LOST_FLAG.write_text(json.dumps({"since": time.time()}))
        end_windows("lost-phone mode on")
    else:
        LOST_FLAG.unlink(missing_ok=True)
    _event("lost_mode_on" if on else "lost_mode_off", who="local")
    _notify_push("ROSE lost-phone mode " + ("ON" if on else "off"),
                 "Every ROSE action now needs the passphrase + a key press on the laptop." if on
                 else "Fingerprint approvals on the phone work again.", "high", "warning")


# ---------------- pages ----------------

_CSP = ("default-src 'none'; script-src 'nonce-{n}'; style-src 'unsafe-inline'; connect-src 'self'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'; img-src 'self' data:")

_JS_HELPERS = r"""
const b64u=b=>{const a=new Uint8Array(b);let s='';for(const c of a)s+=String.fromCharCode(c);return btoa(s).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'')};
const unb64u=s=>{s=s.replace(/-/g,'+').replace(/_/g,'/');s+='==='.slice((s.length+3)%4);return Uint8Array.from(atob(s),c=>c.charCodeAt(0))};
const say=(t,err)=>{const m=document.getElementById('msg');m.textContent=t;m.className=err?'err':'note'};
async function postJSON(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),credentials:'same-origin'});let j={};try{j=await r.json()}catch(e){}return [r.ok,j]}
"""


def headers(nonce: str) -> dict:
    return {"X-Frame-Options": "DENY", "Content-Security-Policy": _CSP.format(n=nonce), "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}


def _page(body: str, nonce: str, script: str = "", refresh: int = 0) -> str:
    from rose import auth
    extra = (".note{color:var(--muted);font-size:.9rem;margin:10px 0 0} details{margin-top:18px} "
             "summary{cursor:pointer;color:var(--muted);font-size:.9rem} .key{font:700 2rem/1 monospace;"
             "padding:8px 14px;border:2px solid var(--accent);border-radius:8px;display:inline-block;margin:6px 0}")
    page = auth._APPROVAL_PAGE.format(body=body).replace("</style>", extra + "</style>", 1)
    if refresh:
        page = page.replace("</title>", f"</title><meta http-equiv='refresh' content='{int(refresh)}'>", 1)
    if script:
        page = page.replace("</body>", f"<script nonce='{nonce}'>{_JS_HELPERS}{script}</script></body>", 1)
    return page


def _json_for_script(obj) -> str:
    return json.dumps(obj).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def approval_page(rec: dict | None, message: str = "") -> tuple[str, str]:
    """Fingerprint approval page; returns (html, CSP nonce)."""
    from rose import guard
    nonce = secrets.token_urlsafe(16)
    if rec is None or rec.get("status") != "pending":
        return _page(f"<h1>ROSE</h1><p>{html.escape(message or 'Nothing to approve.')}</p>", nonce), nonce
    aid = html.escape(rec["id"])
    mins = max(0, int((rec["created"] + guard.APPROVAL_TTL - time.time()) // 60))
    fb = rec.get("fallback") or {}
    head = ("<h1>Approve this action?</h1>"
            f"<p><b>{html.escape(rec['caller'])}</b> asked ROSE to do this on your laptop. Only approve if "
            f"<b>you</b> asked for it just now. Expires in {mins} min.</p>"
            f"<pre>{html.escape(rec['summary'])}</pre>")
    if fb.get("until", 0) > time.time():
        left = int(fb["until"] - time.time())
        body = head + (f"<p>Now press this key on the <b>laptop's own keyboard</b> (within {left} s):</p>"
                       f"<div class='key'>{html.escape(fb['key'])}</div>"
                       "<p class='note'>This page refreshes by itself.</p>")
        return _page(body, nonce, refresh=2), nonce
    err = f"<p id='msg' class='err'>{html.escape(message)}</p>" if message else "<p id='msg' class='note'></p>"
    if rec.get("fallback_error"):
        err += f"<p class='err'>Laptop fallback: {html.escape(rec['fallback_error'])}</p>"
    deny = (f"<form method='post' action='{guard.APPROVE_PATH}'><input type='hidden' name='id' value='{aid}'>"
            "<div class='row'><button class='no' name='action' value='deny'>Deny</button></div></form>")
    fallback = (f"<form method='post' action='{FALLBACK_PATH}'><input type='hidden' name='id' value='{aid}'>"
                "<input type='password' name='passphrase' placeholder='ROSE passphrase' autocomplete='current-password' required>"
                "<div class='row'><button class='ok' name='action' value='fallback'>Continue on the laptop</button></div>"
                "<p class='note'>Then press the key ROSE shows here and on the laptop screen, on the laptop itself.</p></form>")
    lost = bool(rec.get("lost") or lost_armed())
    if lost:
        body = head + "<p>Lost-phone mode is on: approve with your passphrase plus a key press on the laptop.</p>" \
            + fallback + err + deny
        return _page(body, nonce), nonce
    app = ""
    if has_app_key():  # the Rose app's own prompt: one touch, no passkey picker
        app = ("<div class='row'><a class='ok' style='text-align:center;text-decoration:none;padding:12px' href='"
               + html.escape(app_intent("approve", u=f"{public_base()}{guard.APPROVE_PATH}?id={rec['id']}"))
               + "'>Approve in Rose app</a></div>")
    body = (head + app + "<div class='row'><button class='ok' id='fp'>Approve with fingerprint</button></div>" + err + deny
            + "<details><summary>Phone dead or lost?</summary>" + fallback + "</details>")
    script = ("const o=" + _json_for_script(assertion_options(rec)) + ",id=" + _json_for_script(rec["id"]) + ";"
              "document.getElementById('fp').onclick=async()=>{"
              "if(!window.PublicKeyCredential){say('This browser has no passkey support.',1);return}"
              "say('Waiting for your fingerprint…');let c;"
              "try{c=await navigator.credentials.get({publicKey:{challenge:unb64u(o.challenge),rpId:o.rpId,"
              "userVerification:'required',timeout:o.timeout,"
              "allowCredentials:o.allowCredentials.map(x=>({...x,id:unb64u(x.id)}))}})}"
              "catch(e){say('Cancelled or failed: '+e.name+'. Reload to try again.',1);return}"
              "const r=c.response;const [ok,j]=await postJSON('" + WEBAUTHN_PATH + "',{id:id,credentialId:b64u(c.rawId),"
              "clientDataJSON:b64u(r.clientDataJSON),authenticatorData:b64u(r.authenticatorData),signature:b64u(r.signature)});"
              "document.getElementById('fp').disabled=true;say(j.message||(ok?'Done.':'Failed.'),!ok);"
              "if(!ok&&j.retry){setTimeout(()=>location.reload(),2500)}};")
    return _page(body, nonce, script), nonce


def register_page(tok: str, message: str = "") -> tuple[str, str]:
    nonce = secrets.token_urlsafe(16)
    if not registration_valid(tok):
        return _page("<h1>ROSE passkey</h1><p>This registration link expired or was used. Run "
                     "<b>rose fingerprint register</b> on the laptop again.</p>", nonce), nonce
    body = ("<h1>Register this phone with ROSE</h1>"
            "<p>After this, ROSE actions are approved with this phone's fingerprint. Enter the ROSE passphrase, "
            "then confirm with your fingerprint.</p>"
            "<input type='password' id='pw' placeholder='ROSE passphrase' autocomplete='current-password' required autofocus>"
            "<div class='row'><button class='ok' id='go'>Register fingerprint</button></div>"
            f"<p id='msg' class='{'err' if message else 'note'}'>{html.escape(message)}</p>"
            "<p class='note'>Have the Rose app? <a href='"
            + html.escape(app_intent("register", base=public_base(), t=tok))
            + "'>Register it instead</a>: approvals then open its fingerprint prompt directly.</p>")
    script = ("const t=" + _json_for_script(tok) + ";"
              "document.getElementById('go').onclick=async()=>{"
              "if(!window.PublicKeyCredential){say('This browser has no passkey support.',1);return}"
              "say('Checking passphrase…');"
              "let [ok,o]=await postJSON('" + REGISTER_PATH + "/options',{t:t,passphrase:document.getElementById('pw').value});"
              "if(!ok){say(o.message||'Refused.',1);return}"
              "say('Now confirm with your fingerprint…');let c;"
              "try{c=await navigator.credentials.create({publicKey:{...o,challenge:unb64u(o.challenge),"
              "user:{...o.user,id:unb64u(o.user.id)},excludeCredentials:o.excludeCredentials.map(x=>({...x,id:unb64u(x.id)}))}})}"
              "catch(e){say('Cancelled or failed: '+e.name,1);return}"
              "const r=c.response;let [ok2,j]=await postJSON('" + REGISTER_PATH + "/finish',{t:t,"
              "clientDataJSON:b64u(r.clientDataJSON),attestationObject:b64u(r.attestationObject),"
              "transports:(r.getTransports?r.getTransports():[])});"
              "say(j.message||(ok2?'Registered.':'Failed.'),!ok2);if(ok2)document.getElementById('go').disabled=true};")
    return _page(body, nonce, script), nonce


# ---------------- HTTP routes ----------------

def add_routes(mcp) -> None:
    """WebAuthn approval, laptop fallback and passkey registration endpoints (only usable with valid ids/tokens)."""
    from starlette.concurrency import run_in_threadpool
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse

    def html_resp(pair: tuple[str, str], code: int = 200) -> HTMLResponse:
        return HTMLResponse(pair[0], code, headers=headers(pair[1]))

    def json_resp(obj: dict, code: int = 200) -> JSONResponse:
        return JSONResponse(obj, code, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    async def body_json(request: Request) -> dict:
        raw = await request.body()
        if len(raw) > 64 * 1024:
            raise ValueError("too large")
        d = json.loads(raw or b"{}")
        if not isinstance(d, dict):
            raise ValueError("not an object")
        return d

    @mcp.custom_route(WEBAUTHN_PATH, methods=["POST"], include_in_schema=False)
    async def webauthn_post(request: Request):
        try:
            data = await body_json(request)
        except ValueError:
            return json_resp({"message": "Bad request."}, 400)
        try:
            rec = await run_in_threadpool(approve_webauthn, str(data.get("id", "")), data)
        except LookupError as e:
            return json_resp({"message": str(e)}, 400)
        except PermissionError as e:
            return json_resp({"message": str(e), "retry": True}, 401)
        msg = "Approved and done. You can go back to Claude." if rec["status"] == "done" else \
            f"Approved, but the action failed: {str(rec.get('result', ''))[:300]}"
        return json_resp({"message": msg, "status": rec["status"]})

    @mcp.custom_route(FALLBACK_PATH, methods=["POST"], include_in_schema=False)
    async def fallback_post(request: Request):
        from rose import guard
        form = await request.form()
        aid = str(form.get("id", ""))
        try:
            rec = await run_in_threadpool(fallback_start, aid, str(form.get("passphrase", "")))
        except LookupError as e:
            return html_resp(approval_page(None, str(e)), 400)
        except PermissionError as e:
            return html_resp(approval_page(guard.load_approval(aid), str(e)), 401)
        return html_resp(approval_page(rec) if rec["status"] == "pending" else
                         approval_page(None, f"This approval is {rec['status']}."))

    @mcp.custom_route(DEVICE_APPROVE_PATH, methods=["GET"], include_in_schema=False)
    async def device_get(request: Request):
        try:
            return json_resp(await run_in_threadpool(device_options, request.query_params.get("id", "")))
        except LookupError as e:
            return json_resp({"message": str(e)}, 404)
        except PermissionError as e:
            return json_resp({"message": str(e)}, 409)

    @mcp.custom_route(DEVICE_APPROVE_PATH, methods=["POST"], include_in_schema=False)
    async def device_post(request: Request):
        try:
            data = await body_json(request)
        except ValueError:
            return json_resp({"message": "Bad request."}, 400)
        try:
            rec = await run_in_threadpool(approve_device, str(data.get("id", "")), data)
        except LookupError as e:
            return json_resp({"message": str(e)}, 400)
        except PermissionError as e:
            return json_resp({"message": str(e), "retry": True}, 401)
        msg = "Approved and done." if rec["status"] == "done" else \
            f"Approved, but the action failed: {str(rec.get('result', ''))[:300]}"
        return json_resp({"message": msg, "status": rec["status"]})

    @mcp.custom_route(DEVICE_REGISTER_PATH, methods=["POST"], include_in_schema=False)
    async def device_register_post(request: Request):
        try:
            data = await body_json(request)
            res = await run_in_threadpool(device_register, str(data.get("t", "")), str(data.get("passphrase", "")),
                                          str(data.get("public_key", "")), str(data.get("device", ""))[:60])
        except ValueError:
            return json_resp({"message": "Bad request."}, 400)
        except LookupError as e:
            return json_resp({"message": str(e)}, 400)
        except PermissionError as e:
            return json_resp({"message": str(e)}, 401)
        return json_resp({"message": f"Registered as '{res['name']}'.", **res})

    # ---- Rose app security screen (X-Rose-Session header carries the management session) ----

    def session_of(request: Request) -> str:
        return request.headers.get("x-rose-session", "")

    async def manage_call(fn, *args):
        try:
            return json_resp(await run_in_threadpool(fn, *args))
        except LookupError as e:
            return json_resp({"message": str(e)}, 404)
        except PermissionError as e:
            return json_resp({"message": str(e)}, 401)
        except ValueError as e:
            return json_resp({"message": str(e)}, 400)

    @mcp.custom_route(DEVICE_CHALLENGE_PATH, methods=["GET"], include_in_schema=False)
    async def manage_challenge_get(request: Request):
        return await manage_call(manage_challenge, request.query_params.get("key_id", ""))

    @mcp.custom_route(DEVICE_SESSION_PATH, methods=["POST"], include_in_schema=False)
    async def manage_session_post(request: Request):
        try:
            data = await body_json(request)
        except ValueError:
            return json_resp({"message": "Bad request."}, 400)
        return await manage_call(manage_open, data)

    @mcp.custom_route(DEVICE_KEYS_PATH, methods=["GET"], include_in_schema=False)
    async def manage_keys_get(request: Request):
        return await manage_call(manage_keys, session_of(request))

    @mcp.custom_route(DEVICE_REMOVE_PATH, methods=["POST"], include_in_schema=False)
    async def manage_remove_post(request: Request):
        try:
            data = await body_json(request)
        except ValueError:
            return json_resp({"message": "Bad request."}, 400)
        return await manage_call(manage_remove, session_of(request), str(data.get("name", ""))[:60])

    @mcp.custom_route(DEVICE_REKEY_PATH, methods=["POST"], include_in_schema=False)
    async def manage_rekey_post(request: Request):
        try:
            data = await body_json(request)
        except ValueError:
            return json_resp({"message": "Bad request."}, 400)
        return await manage_call(manage_rekey, session_of(request), str(data.get("passphrase", "")),
                                 str(data.get("public_key", "")))

    @mcp.custom_route(DEVICE_PASSPHRASE_PATH, methods=["POST"], include_in_schema=False)
    async def manage_passphrase_post(request: Request):
        try:
            data = await body_json(request)
        except ValueError:
            return json_resp({"message": "Bad request."}, 400)
        return await manage_call(manage_passphrase, session_of(request), str(data.get("current", "")),
                                 str(data.get("new", ""))[:256])

    @mcp.custom_route(REGISTER_PATH, methods=["GET"], include_in_schema=False)
    async def register_get(request: Request):
        tok = request.query_params.get("t", "")
        return html_resp(register_page(tok), 200 if registration_valid(tok) else 404)

    @mcp.custom_route(REGISTER_PATH + "/options", methods=["POST"], include_in_schema=False)
    async def register_options(request: Request):
        try:
            data = await body_json(request)
            opts = await run_in_threadpool(registration_options, str(data.get("t", "")), str(data.get("passphrase", "")))
        except ValueError:
            return json_resp({"message": "Bad request."}, 400)
        except LookupError as e:
            return json_resp({"message": str(e)}, 400)
        except PermissionError as e:
            return json_resp({"message": str(e)}, 401)
        return json_resp(opts)

    @mcp.custom_route(REGISTER_PATH + "/finish", methods=["POST"], include_in_schema=False)
    async def register_finish(request: Request):
        try:
            data = await body_json(request)
            res = await run_in_threadpool(registration_finish, str(data.get("t", "")), data)
        except ValueError:
            return json_resp({"message": "Bad request."}, 400)
        except LookupError as e:
            return json_resp({"message": str(e)}, 400)
        except PermissionError as e:
            return json_resp({"message": str(e)}, 401)
        return json_resp({"message": f"Registered as '{res['name']}'. Test it on the laptop: rose fingerprint test"})


# ---------------- CLI ----------------

def cli(argv: list[str]) -> int:  # noqa: C901 (one branch per subcommand)
    import getpass
    import shutil
    import subprocess

    cmd, args = (argv[0] if argv else "status"), argv[1:]
    if cmd == "register":
        name = args[args.index("--name") + 1] if "--name" in args and args.index("--name") + 1 < len(args) else "phone"
        if not get_public_url():
            print("No public URL yet (start the tunnel: rose tunnel). WebAuthn needs the https URL.")
            return 1
        url = new_registration(name)
        print(f"Open this on your phone within 10 minutes (one use):\n\n  {url}\n")
        if shutil.which("qrencode"):
            subprocess.run(["qrencode", "-t", "ansiutf8", url])
        try:
            pushmod.push("🔑 Register ROSE passkey", "Tap to add this phone's fingerprint to ROSE.", url=url,
                         priority="high", tags="key", desktop=False)
            time.sleep(2)  # the push is sent from a background thread
        except Exception:
            pass
        print("Then test it: rose fingerprint test")
    elif cmd == "register-app":
        name = args[args.index("--name") + 1] if "--name" in args and args.index("--name") + 1 < len(args) else "phone"
        if not get_public_url():
            print("No public URL yet (start the tunnel: rose tunnel). The app needs the https URL.")
            return 1
        tok = new_registration(name).split("t=", 1)[1]
        link = app_link("register", base=public_base(), t=tok)
        print(f"On the phone with Rose Voice installed, tap the push or open within 10 minutes (one use):\n\n  {link}\n")
        if shutil.which("qrencode"):
            subprocess.run(["qrencode", "-t", "ansiutf8", link])
        try:
            pushmod.push("🔑 Register Rose app fingerprint", "Tap to let the Rose app approve ROSE actions.", url=link,
                         priority="high", tags="key", desktop=False)
            time.sleep(2)
        except Exception:
            pass
        print("Then test it: rose fingerprint test")
    elif cmd == "list":
        keys = list_passkeys() + list_device_keys()
        if not keys:
            print("No passkeys. Register one: rose fingerprint register --name phone")
        for k in keys:
            used = time.strftime("%Y-%m-%d %H:%M", time.localtime(k["last_used"])) if k["last_used"] else "never"
            print(f"{k['name']:20} alg {k['alg']:>5}  created {time.strftime('%Y-%m-%d %H:%M', time.localtime(k['created']))}"
                  f"  last used {used}")
    elif cmd == "remove":
        if len(args) != 1:
            print("Usage: rose fingerprint remove <name>")
            return 1
        n = remove_passkey(args[0])
        print(f"Removed {n} passkey(s)." if n else f"No passkey named {args[0]}.")
        if n and enabled() and not list_passkeys() and not has_app_key():
            print("WARNING: fingerprint mode is on but no passkey is left: only the laptop fallback can approve.")
    elif cmd == "status":
        st = status()
        print(f"Approval mode: {st['mode']}")
        print(f"Passkeys: {', '.join(st['passkeys']) or 'none'}")
        print(f"Phone (last seen): {st['phone']}    Lost-phone mode: {'ON' if st['lost_mode'] else 'off'}")
        print(f"Window after a fingerprint: {st['window_minutes']} min")
        for who, left in st["windows"].items():
            print(f"  open window: {who} ({left} s left)")
    elif cmd == "test":
        if not list_passkeys() and not has_app_key():
            print("Register a passkey first: rose fingerprint register")
            return 1
        res = _new_approval(TEST_TOOL, {}, "rose fingerprint test", fresh=True, test=True)
        print(f"Sent to your phone. Or open:\n\n  {res['approval_url']}\n\nWaiting up to 3 minutes...")
        from rose import guard
        end = time.time() + 180
        while time.time() < end:
            rec = guard.load_approval(res["approval_id"])
            if rec and rec["status"] != "pending":
                print(f"Result: {rec['status']} {rec.get('result', '')}")
                return 0 if rec["status"] == "done" else 1
            time.sleep(2)
        print("No answer in 3 minutes.")
        return 1
    elif cmd == "enable":
        if not list_passkeys() and not has_app_key():
            print("Refusing: no passkey registered. Run: rose fingerprint register, then rose fingerprint test.")
            return 1
        from rose.config import update_settings
        update_settings({"approval_mode": "fingerprint"})
        print("Fingerprint approvals ON for remote clients. Phone lock state now: " + lock_state())
        print("rose-watch polls the phone every 2 s (restart it if it was already running). Undo: rose fingerprint disable")
    elif cmd == "disable":
        from rose.config import update_settings
        update_settings({"approval_mode": "passphrase"})
        end_windows("fingerprint disabled")
        print("Back to passphrase approvals.")
    elif cmd == "lost":
        if not args or args[0] not in ("on", "off"):
            print(f"Lost-phone mode is {'ON' if lost_armed() else 'off'}. Usage: rose fingerprint lost on|off")
            return 0
        if not local_physical_check(getpass.getpass("ROSE passphrase: ")):
            print("Not confirmed (wrong passphrase or no key press). Nothing changed.")
            return 1
        set_lost(args[0] == "on")
        print("Lost-phone mode " + ("ON: every remote action needs passphrase + a laptop key press."
                                    if args[0] == "on" else "off."))
    else:
        print("Usage: rose fingerprint register [--name N] | register-app [--name N] | test | enable | disable | status | list | "
              "remove <name> | lost on|off")
        return 1
    return 0
