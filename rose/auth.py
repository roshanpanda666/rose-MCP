"""ROSE authentication for the HTTP transport.

Two ways in, both required before the HTTP server will start:

* OAuth 2.1 (for claude.ai custom connectors / the phone). Clients register themselves
  (dynamic client registration), but only with redirect URIs on allowlisted hosts, and every
  authorization must be approved on ROSE's login page with the owner passphrase.
* Static bearer tokens (for Claude Code, scripts). Created and revoked with the CLI.

Secrets are never stored in plain text: the passphrase is scrypt-hashed, tokens are SHA-256 hashed.

CLI:
    uv run python -m rose.auth set-passphrase
    uv run python -m rose.auth new-token <name>
    uv run python -m rose.auth list
    uv run python -m rose.auth revoke <name | client_id | all>
    uv run python -m rose.auth label <client_id | token-name> <label>   # sender name on the bridge
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import secrets
import sys
import threading
import time
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from rose.common import _log
from rose.config import ROSE_HOME, load_config

AUTH_DIR = ROSE_HOME / "auth"
STATE_PATH = AUTH_DIR / "state.json"
SCOPE = "rose"
LOGIN_PATH = "/rose/login"

ACCESS_TTL = 3600  # 1 hour
REFRESH_TTL = 30 * 24 * 3600  # 30 days
CODE_TTL = 300  # 5 minutes
PENDING_TTL = 600  # 10 minutes to type the passphrase
MAX_FAILURES = 5  # wrong passphrases ...
LOCKOUT_WINDOW = 15 * 60  # ... within this window lock the login page for this long

DEFAULT_REDIRECT_HOSTS = ["claude.ai", "claude.com", "localhost", "127.0.0.1"]


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------- persistent state ----------------

_lock = threading.Lock()


def _empty_state() -> dict:
    return {"passphrase": None, "static_tokens": {}, "clients": {}, "access": {}, "refresh": {}, "labels": {}}


def client_label(client_id: str) -> str:
    """Human name for an authenticated client (set with `rose.auth label`), used as bridge sender."""
    s = load_state()
    if client_id in s["labels"]:
        return s["labels"][client_id]
    if client_id.startswith("static:"):
        return f"token:{client_id[7:]}"
    name = (s["clients"].get(client_id) or {}).get("client_name") or "client"
    return f"{name} ({client_id[:8]})"


def load_state() -> dict:
    if not STATE_PATH.exists():
        return _empty_state()
    return {**_empty_state(), **json.loads(STATE_PATH.read_text())}


def save_state(state: dict) -> None:
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(AUTH_DIR, 0o700)
    tmp = STATE_PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def _hash_passphrase(p: str, salt: bytes | None = None) -> dict:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(p.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024)
    return {"salt": salt.hex(), "hash": dk.hex()}


def check_passphrase(p: str) -> bool:
    stored = load_state()["passphrase"]
    if not stored:
        return False
    candidate = _hash_passphrase(p, bytes.fromhex(stored["salt"]))["hash"]
    return hmac.compare_digest(candidate, stored["hash"])


def auth_configured() -> bool:
    s = load_state()
    return bool(s["passphrase"]) or bool(s["static_tokens"])


def redirect_host_allowed(uri: str) -> bool:
    u = urlparse(uri)
    hosts = load_config().get("oauth_redirect_hosts", DEFAULT_REDIRECT_HOSTS)
    host = (u.hostname or "").lower()
    if host in ("localhost", "127.0.0.1"):
        return host in hosts and u.scheme in ("http", "https")
    # Remote redirect targets must be https and on an allowlisted host (or its subdomain).
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in hosts)


# ---------------- OAuth provider ----------------

class RoseOAuthProvider:
    """Single-owner OAuth authorization server. Approval requires the ROSE passphrase."""

    def __init__(self, public_url: str):
        self.public_url = public_url.rstrip("/")
        self._pending: dict[str, tuple[float, str, AuthorizationParams]] = {}  # id -> (expiry, client_id, params)
        self._codes: dict[str, AuthorizationCode] = {}
        self._failures: list[float] = []

    # --- clients ---
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        c = load_state()["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(c) if c else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        bad = [str(u) for u in (client_info.redirect_uris or []) if not redirect_host_allowed(str(u))]
        if bad or not client_info.redirect_uris:
            _log({"event": "oauth_register_refused", "redirect_uris": bad or None})
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="Redirect URI host not allowed by ROSE: " + ", ".join(bad),
            )
        with _lock:
            s = load_state()
            s["clients"][client_info.client_id] = client_info.model_dump(mode="json")
            save_state(s)
        _log({"event": "oauth_client_registered", "client_id": client_info.client_id,
              "client_name": client_info.client_name, "redirect_uris": [str(u) for u in client_info.redirect_uris]})

    # --- authorization: park the request and send the browser to the passphrase page ---
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not redirect_host_allowed(str(params.redirect_uri)):
            raise AuthorizeError(error="invalid_request", error_description="Redirect URI not allowed")
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if v[0] > now}
        req_id = secrets.token_urlsafe(32)
        self._pending[req_id] = (now + PENDING_TTL, client.client_id, params)
        return f"{self.public_url}{LOGIN_PATH}?req={req_id}"

    def pending(self, req_id: str):
        v = self._pending.get(req_id)
        if not v or v[0] < time.time():
            return None
        return v

    def locked_out(self) -> bool:
        now = time.time()
        self._failures = [t for t in self._failures if now - t < LOCKOUT_WINDOW]
        return len(self._failures) >= MAX_FAILURES

    def approve(self, req_id: str, passphrase: str) -> str:
        """Check the passphrase; on success return the client redirect URL carrying the code."""
        if self.locked_out():
            raise PermissionError("Too many wrong passphrases. Try again in 15 minutes.")
        v = self.pending(req_id)
        if not v:
            raise LookupError("This sign-in request expired. Start the connection again from Claude.")
        _, client_id, params = v
        if not check_passphrase(passphrase):
            self._failures.append(time.time())
            _log({"event": "oauth_login_failed", "client_id": client_id})
            raise PermissionError("Wrong passphrase.")
        del self._pending[req_id]
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [SCOPE], expires_at=time.time() + CODE_TTL,
            client_id=client_id, code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource, subject="owner",
        )
        _log({"event": "oauth_login_approved", "client_id": client_id})
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    def deny(self, req_id: str) -> str | None:
        v = self._pending.pop(req_id, None)
        if not v:
            return None
        return construct_redirect_uri(str(v[2].redirect_uri), error="access_denied", state=v[2].state)

    # --- codes and tokens ---
    async def load_authorization_code(self, client, authorization_code: str):
        c = self._codes.get(authorization_code)
        if c and c.client_id == client.client_id and c.expires_at > time.time():
            return c
        return None

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        with _lock:
            s = load_state()
            s["access"] = {k: v for k, v in s["access"].items() if v["expires_at"] > now}
            s["refresh"] = {k: v for k, v in s["refresh"].items() if v["expires_at"] > now}
            s["access"][_h(access)] = {"client_id": client_id, "scopes": scopes, "resource": resource,
                                       "expires_at": now + ACCESS_TTL}
            s["refresh"][_h(refresh)] = {"client_id": client_id, "scopes": scopes, "resource": resource,
                                         "expires_at": now + REFRESH_TTL}
            save_state(s)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          scope=" ".join(scopes), refresh_token=refresh)

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError(error="invalid_grant", error_description="Code already used or unknown")
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client, refresh_token: str):
        r = load_state()["refresh"].get(_h(refresh_token))
        if r and r["client_id"] == client.client_id and r["expires_at"] > time.time():
            return RefreshToken(token=refresh_token, client_id=r["client_id"], scopes=r["scopes"],
                                expires_at=r["expires_at"], resource=r.get("resource"), subject="owner")
        return None

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        with _lock:
            s = load_state()
            if s["refresh"].pop(_h(refresh_token.token), None) is None:  # rotate: old one is single-use
                raise TokenError(error="invalid_grant", error_description="Refresh token already used")
            save_state(s)
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        s = load_state()
        h = _h(token)
        st = s["static_tokens"].get(h)
        if st:
            return AccessToken(token=token, client_id=f"static:{st['name']}", scopes=[SCOPE], subject="owner")
        a = s["access"].get(h)
        if a and a["expires_at"] > time.time() and a["client_id"] in s["clients"]:
            return AccessToken(token=token, client_id=a["client_id"], scopes=a["scopes"],
                               expires_at=a["expires_at"], resource=a.get("resource"), subject="owner")
        return None

    async def revoke_token(self, token) -> None:
        with _lock:
            s = load_state()
            s["access"].pop(_h(token.token), None)
            s["refresh"].pop(_h(token.token), None)
            save_state(s)


# ---------------- login page ----------------

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ROSE sign-in</title>
<style>
:root{{color-scheme:light dark;--bg:#f6f6f4;--fg:#1d1d1b;--card:#fff;--muted:#6b6b66;--accent:#c2410c;--err:#b91c1c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecece8;--card:#1f1f1d;--muted:#9a9a94;--accent:#fb923c;--err:#f87171}}}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif;padding:16px;box-sizing:border-box}}
main{{background:var(--card);border-radius:14px;padding:28px;max-width:380px;width:100%;box-shadow:0 2px 16px #0002}}
h1{{font-size:1.25rem;margin:0 0 4px}} p{{color:var(--muted);margin:0 0 16px;font-size:.92rem}}
b{{color:var(--fg)}} input{{width:100%;box-sizing:border-box;padding:10px 12px;font:inherit;border-radius:8px;border:1px solid #8885;background:transparent;color:inherit}}
.row{{display:flex;gap:8px;margin-top:14px}} button{{flex:1;padding:10px;font:inherit;border-radius:8px;border:0;cursor:pointer}}
.ok{{background:var(--accent);color:#fff}} .no{{background:#8882;color:inherit}} .err{{color:var(--err);font-size:.9rem;margin:10px 0 0}}
</style></head><body><main>
<h1>Connect to ROSE?</h1>
<p><b>{client}</b> wants control of this computer (screen, clipboard, files, apps, input, webcam). It will return to <b>{host}</b>.</p>
{body}
</main></body></html>"""


def login_page(client_name: str, redirect_host: str, req_id: str, error: str = "", done: bool = False) -> str:
    if done:
        body = f"<p class='err'>{html.escape(error)}</p>"
    else:
        body = (
            f"<form method='post' action='{LOGIN_PATH}'>"
            f"<input type='hidden' name='req' value='{html.escape(req_id)}'>"
            "<input type='password' name='passphrase' placeholder='ROSE passphrase' autocomplete='current-password' autofocus required>"
            + (f"<p class='err'>{html.escape(error)}</p>" if error else "")
            + "<div class='row'><button class='no' name='action' value='deny' formnovalidate>Deny</button>"
            "<button class='ok' name='action' value='allow'>Allow</button></div></form>"
        )
    return _PAGE.format(client=html.escape(client_name), host=html.escape(redirect_host), body=body)


_APPROVAL_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ROSE approval</title>
<style>
:root{{color-scheme:light dark;--bg:#f6f6f4;--fg:#1d1d1b;--card:#fff;--muted:#6b6b66;--accent:#c2410c;--err:#b91c1c;--code:#0001}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecece8;--card:#1f1f1d;--muted:#9a9a94;--accent:#fb923c;--err:#f87171;--code:#fff1}}}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif;padding:16px;box-sizing:border-box}}
main{{background:var(--card);border-radius:14px;padding:24px;max-width:460px;width:100%;box-shadow:0 2px 16px #0002}}
h1{{font-size:1.2rem;margin:0 0 6px}} p{{color:var(--muted);margin:0 0 12px;font-size:.92rem}} b{{color:var(--fg)}}
pre{{background:var(--code);border-radius:8px;padding:10px;white-space:pre-wrap;word-break:break-word;font-size:.85rem;margin:0 0 14px}}
input{{width:100%;box-sizing:border-box;padding:10px 12px;font:inherit;border-radius:8px;border:1px solid #8885;background:transparent;color:inherit}}
.row{{display:flex;gap:8px;margin-top:14px}} button{{flex:1;padding:10px;font:inherit;border-radius:8px;border:0;cursor:pointer}}
.ok{{background:var(--accent);color:#fff}} .no{{background:#8882;color:inherit}} .err{{color:var(--err);font-size:.9rem;margin:10px 0 0}}
</style></head><body><main>{body}</main></body></html>"""


def approval_page(rec: dict | None, message: str = "") -> str:
    """Approval page: shows exactly what will run and who asked; approve needs the ROSE passphrase."""
    if rec is None or rec.get("status") != "pending":
        return _APPROVAL_PAGE.format(body=f"<h1>ROSE</h1><p>{html.escape(message)}</p>")
    mins = max(0, int((rec["created"] + 600 - time.time()) // 60))
    body = (
        "<h1>Approve this action?</h1>"
        f"<p><b>{html.escape(rec['caller'])}</b> asked ROSE to do this on your laptop. Only approve if "
        f"<b>you</b> asked for it just now. Expires in {mins} min.</p>"
        f"<pre>{html.escape(rec['summary'])}</pre>"
        f"<form method='post' action='/rose/approve'><input type='hidden' name='id' value='{html.escape(rec['id'])}'>"
        "<input type='password' name='passphrase' placeholder='ROSE passphrase' autocomplete='current-password' required autofocus>"
        + (f"<p class='err'>{html.escape(message)}</p>" if message else "")
        + "<div class='row'><button class='no' name='action' value='deny' formnovalidate>Deny</button>"
        "<button class='ok' name='action' value='approve'>Approve</button></div></form>"
    )
    return _APPROVAL_PAGE.format(body=body)


# ---------------- CLI ----------------

def _cli(argv: list[str]) -> int:
    import getpass

    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    with _lock:
        s = load_state()
        if cmd == "set-passphrase":
            p1 = getpass.getpass("New ROSE passphrase (min 12 chars): ")
            if len(p1) < 12:
                print("Too short.")
                return 1
            if getpass.getpass("Repeat: ") != p1:
                print("Didn't match.")
                return 1
            s["passphrase"] = _hash_passphrase(p1)
            save_state(s)
            print("Passphrase set.")
        elif cmd == "new-token":
            if len(args) != 1:
                print("Usage: new-token <name>")
                return 1
            name = args[0]
            if any(t["name"] == name for t in s["static_tokens"].values()):
                print(f"A token named '{name}' exists; revoke it first.")
                return 1
            tok = "rose_" + secrets.token_urlsafe(32)
            s["static_tokens"][_h(tok)] = {"name": name, "created": int(time.time())}
            save_state(s)
            print(f"Token '{name}' (shown once, store it safely):\n{tok}")
        elif cmd == "list":
            print("Passphrase:", "set" if s["passphrase"] else "NOT SET")
            print("Static tokens:", ", ".join(t["name"] for t in s["static_tokens"].values()) or "none")
            now = time.time()
            for cid, c in s["clients"].items():
                live = sum(1 for r in s["refresh"].values() if r["client_id"] == cid and r["expires_at"] > now)
                label = s["labels"].get(cid, "no label")
                print(f"OAuth client {cid}: {c.get('client_name') or '?'} [{label}] ({live} active sessions)")
        elif cmd == "label":
            if len(args) != 2:
                print("Usage: label <client_id | token-name> <label>")
                return 1
            target, label = args
            if target not in s["clients"]:
                names = {t["name"] for t in s["static_tokens"].values()}
                if target not in names:
                    print(f"No OAuth client or token named {target}. See: list")
                    return 1
                target = f"static:{target}"
            s["labels"][target] = label
            save_state(s)
            print(f"{target} is now labelled '{label}'.")
        elif cmd == "revoke":
            if len(args) != 1:
                print("Usage: revoke <name | client_id | all>")
                return 1
            t = args[0]
            if t == "all":
                s.update(static_tokens={}, clients={}, access={}, refresh={})
            else:
                s["static_tokens"] = {k: v for k, v in s["static_tokens"].items() if v["name"] != t}
                s["clients"].pop(t, None)
                s["access"] = {k: v for k, v in s["access"].items() if v["client_id"] != t}
                s["refresh"] = {k: v for k, v in s["refresh"].items() if v["client_id"] != t}
            save_state(s)
            print(f"Revoked {t}.")
        else:
            print(f"Unknown command {cmd}.\n{__doc__}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
