"""Minimal WebAuthn (passkey) verification for ROSE's fingerprint approvals.

Only what ROSE needs, written to be read and audited:

* a small strict CBOR decoder (no tags, no floats, no indefinite lengths, no duplicate map keys),
* authenticator-data parsing,
* registration checks (attestation "none": the passphrase on the registration page vouches for the key),
* assertion checks (type, challenge, origin, rpIdHash, UP + UV flags, signature, signCount).

Signatures are checked with `cryptography`: ES256 (-7, P-256), EdDSA (-8, Ed25519) and RS256 (-257).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

ES256, EDDSA, RS256 = -7, -8, -257
ALGORITHMS = (ES256, EDDSA, RS256)

FLAG_UP, FLAG_UV, FLAG_AT, FLAG_ED = 0x01, 0x04, 0x40, 0x80


class WebAuthnError(ValueError):
    """A registration or assertion failed a check (message says which, never secrets)."""


# ---------------- base64url ----------------

def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_dec(s: str) -> bytes:
    if not isinstance(s, str) or len(s) > 1_000_000:
        raise WebAuthnError("bad base64url value")
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (ValueError, TypeError) as e:
        raise WebAuthnError("bad base64url value") from e


# ---------------- CBOR (strict subset) ----------------

_MAX_DEPTH = 16


def _cbor_item(b: bytes, i: int, depth: int) -> tuple[Any, int]:
    if depth > _MAX_DEPTH:
        raise WebAuthnError("CBOR nested too deep")
    if i >= len(b):
        raise WebAuthnError("CBOR truncated")
    ib = b[i]
    mt, ai = ib >> 5, ib & 0x1F
    i += 1
    if ai < 24:
        val = ai
    elif ai in (24, 25, 26, 27):
        n = 1 << (ai - 24)
        if i + n > len(b):
            raise WebAuthnError("CBOR truncated")
        val = int.from_bytes(b[i:i + n], "big")
        i += n
    else:
        raise WebAuthnError("CBOR indefinite length or reserved value not allowed")
    if mt == 0:
        return val, i
    if mt == 1:
        return -1 - val, i
    if mt in (2, 3):
        if i + val > len(b):
            raise WebAuthnError("CBOR truncated")
        raw = b[i:i + val]
        i += val
        if mt == 2:
            return bytes(raw), i
        try:
            return raw.decode("utf-8"), i
        except UnicodeDecodeError as e:
            raise WebAuthnError("CBOR text is not UTF-8") from e
    if mt == 4:
        if val > len(b):
            raise WebAuthnError("CBOR array too long")
        out = []
        for _ in range(val):
            item, i = _cbor_item(b, i, depth + 1)
            out.append(item)
        return out, i
    if mt == 5:
        if val > len(b):
            raise WebAuthnError("CBOR map too long")
        m: dict = {}
        for _ in range(val):
            k, i = _cbor_item(b, i, depth + 1)
            if not isinstance(k, (int, str, bytes)):
                raise WebAuthnError("CBOR map key type not allowed")
            if k in m:
                raise WebAuthnError("CBOR duplicate map key")
            m[k], i = _cbor_item(b, i, depth + 1)
        return m, i
    if mt == 7:
        simple = {20: False, 21: True, 22: None}
        if ai in simple:
            return simple[ai], i
        raise WebAuthnError("CBOR float/simple value not allowed")
    raise WebAuthnError("CBOR tags not allowed")


def cbor_decode_prefix(b: bytes) -> tuple[Any, int]:
    """Decode one CBOR item at the start of `b`; returns (value, bytes used)."""
    return _cbor_item(bytes(b), 0, 0)


def cbor_decode(b: bytes) -> Any:
    """Decode exactly one CBOR item; trailing bytes are an error."""
    val, used = cbor_decode_prefix(b)
    if used != len(b):
        raise WebAuthnError("CBOR has trailing bytes")
    return val


# ---------------- authenticator data ----------------

def parse_auth_data(ad: bytes) -> dict:
    """rpIdHash, flags, signCount and (when AT is set) the attested credential id + COSE key bytes."""
    if len(ad) < 37:
        raise WebAuthnError("authenticator data too short")
    out = {"rp_id_hash": ad[:32], "flags": ad[32], "sign_count": struct.unpack(">I", ad[33:37])[0]}
    rest = ad[37:]
    if out["flags"] & FLAG_AT:
        if len(rest) < 18:
            raise WebAuthnError("attested credential data truncated")
        n = struct.unpack(">H", rest[16:18])[0]
        if n == 0 or n > 1023 or len(rest) < 18 + n:
            raise WebAuthnError("bad credential id length")
        out["aaguid"] = rest[:16]
        out["cred_id"] = rest[18:18 + n]
        key, used = cbor_decode_prefix(rest[18 + n:])
        if not isinstance(key, dict):
            raise WebAuthnError("credential public key is not a COSE map")
        out["cose_key"] = rest[18 + n:18 + n + used]
        rest = rest[18 + n + used:]
    if out["flags"] & FLAG_ED:
        ext, used = cbor_decode_prefix(rest)
        if not isinstance(ext, dict):
            raise WebAuthnError("extensions are not a map")
        rest = rest[used:]
    if rest:
        raise WebAuthnError("authenticator data has trailing bytes")
    return out


# ---------------- COSE keys ----------------

def cose_alg(cose_bytes: bytes) -> int:
    key = cbor_decode(cose_bytes)
    alg = key.get(3) if isinstance(key, dict) else None
    if alg not in ALGORITHMS:
        raise WebAuthnError(f"unsupported key algorithm {alg}")
    return alg


def _public_key(cose_bytes: bytes):
    key = cbor_decode(cose_bytes)
    if not isinstance(key, dict):
        raise WebAuthnError("COSE key is not a map")
    kty, alg = key.get(1), key.get(3)
    if alg == ES256 and kty == 2 and key.get(-1) == 1:
        x, y = key.get(-2), key.get(-3)
        if not (isinstance(x, bytes) and isinstance(y, bytes) and len(x) == 32 and len(y) == 32):
            raise WebAuthnError("bad P-256 key")
        try:
            return alg, ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"),
                                                      ec.SECP256R1()).public_key()
        except ValueError as e:
            raise WebAuthnError("P-256 point not on curve") from e
    if alg == EDDSA and kty == 1 and key.get(-1) == 6:
        x = key.get(-2)
        if not (isinstance(x, bytes) and len(x) == 32):
            raise WebAuthnError("bad Ed25519 key")
        return alg, ed25519.Ed25519PublicKey.from_public_bytes(x)
    if alg == RS256 and kty == 3:
        n, e = key.get(-1), key.get(-2)
        if not (isinstance(n, bytes) and isinstance(e, bytes) and len(n) >= 256):
            raise WebAuthnError("bad RSA key (needs >= 2048 bits)")
        return alg, rsa.RSAPublicNumbers(int.from_bytes(e, "big"), int.from_bytes(n, "big")).public_key()
    raise WebAuthnError(f"unsupported COSE key (kty={kty}, alg={alg})")


def verify_signature(cose_bytes: bytes, signature: bytes, data: bytes) -> None:
    alg, pub = _public_key(cose_bytes)
    try:
        if alg == ES256:
            pub.verify(signature, data, ec.ECDSA(hashes.SHA256()))
        elif alg == EDDSA:
            pub.verify(signature, data)
        else:
            pub.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as e:
        raise WebAuthnError("signature does not verify") from e


# ---------------- ceremonies ----------------

def _client_data(raw: bytes, typ: str, challenge: str, origin: str) -> dict:
    try:
        cd = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise WebAuthnError("clientDataJSON is not JSON") from e
    if not isinstance(cd, dict) or cd.get("type") != typ:
        raise WebAuthnError(f"clientData type is not {typ}")
    got = cd.get("challenge")
    if not isinstance(got, str) or not challenge or not hmac.compare_digest(got.rstrip("="), challenge.rstrip("=")):
        raise WebAuthnError("challenge mismatch")
    if cd.get("origin") != origin:
        raise WebAuthnError("origin mismatch")
    if cd.get("crossOrigin") is True:
        raise WebAuthnError("cross-origin request not allowed")
    return cd


def _check_flags(auth: dict, rp_id: str) -> None:
    if not hmac.compare_digest(auth["rp_id_hash"], hashlib.sha256(rp_id.encode()).digest()):
        raise WebAuthnError("rpIdHash mismatch")
    if not auth["flags"] & FLAG_UP:
        raise WebAuthnError("user presence flag missing")
    if not auth["flags"] & FLAG_UV:
        raise WebAuthnError("user verification (fingerprint/PIN) flag missing")


def verify_registration(client_data_json: bytes, attestation_object: bytes, challenge: str, origin: str,
                        rp_id: str) -> dict:
    """Checks a navigator.credentials.create() result; returns {cred_id, cose_key, alg, sign_count}."""
    _client_data(client_data_json, "webauthn.create", challenge, origin)
    att = cbor_decode(attestation_object)
    if not isinstance(att, dict) or not isinstance(att.get("authData"), bytes) or not isinstance(att.get("fmt"), str):
        raise WebAuthnError("bad attestation object")
    auth = parse_auth_data(att["authData"])
    _check_flags(auth, rp_id)
    if not auth["flags"] & FLAG_AT or "cred_id" not in auth:
        raise WebAuthnError("no attested credential data")
    alg = cose_alg(auth["cose_key"])
    _public_key(auth["cose_key"])  # parses and validates the key now, not at first use
    return {"cred_id": auth["cred_id"], "cose_key": auth["cose_key"], "alg": alg, "sign_count": auth["sign_count"],
            "fmt": att["fmt"]}


def verify_assertion(client_data_json: bytes, authenticator_data: bytes, signature: bytes, challenge: str,
                     origin: str, rp_id: str, cose_key: bytes, stored_sign_count: int) -> int:
    """Checks a navigator.credentials.get() result against the stored key; returns the new signCount."""
    _client_data(client_data_json, "webauthn.get", challenge, origin)
    auth = parse_auth_data(authenticator_data)
    _check_flags(auth, rp_id)
    verify_signature(cose_key, signature, authenticator_data + hashlib.sha256(client_data_json).digest())
    new = auth["sign_count"]
    if new and stored_sign_count and new <= stored_sign_count:
        raise WebAuthnError("signature counter went backwards (possible cloned key)")
    return new
