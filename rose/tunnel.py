"""Public tunnels to the local ROSE server: ngrok static domain (permanent URL) or Cloudflare quick
tunnel (random URL each start). Both fail closed: if the public URL doesn't answer an
unauthenticated MCP request with 401, the tunnel is shut down.
"""

from __future__ import annotations

import json
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from rose.config import LOG_DIR, ROSE_HOME, get_public_url, load_config, set_public_url

NGROK_CONFIGS = [Path.home() / ".config/ngrok/ngrok.yml", Path.home() / ".ngrok2/ngrok.yml"]
DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def provider() -> str:
    return load_config().get("tunnel_provider", "cloudflare")


def is_permanent() -> bool:
    return provider() == "ngrok"


def ngrok_has_authtoken() -> bool:
    for p in NGROK_CONFIGS:
        try:
            if re.search(r"^\s*authtoken:\s*\S+", p.read_text(), re.MULTILINE):
                return True
        except OSError:
            pass
    return False


def readiness() -> list[tuple[str, bool, str]]:
    """Setup checklist for the selected provider: (step, done, how to fix)."""
    from rose import auth

    steps = [("ROSE passphrase set", auth.load_state()["passphrase"] is not None, "rose auth set-passphrase")]
    if provider() == "ngrok":
        dom = load_config().get("ngrok_domain", "")
        steps += [
            ("ngrok installed", bool(shutil.which("ngrok")), "see README → Remote access with ngrok"),
            ("ngrok authtoken added", ngrok_has_authtoken(), "ngrok config add-authtoken <token>"),
            ("static domain set", bool(dom and DOMAIN_RE.match(dom)), "paste your *.ngrok-free.app domain"),
        ]
    else:
        steps += [("cloudflared installed", bool(shutil.which("cloudflared")), "sudo apt install cloudflared")]
    return steps


def _code(url: str, method="GET", data=None, headers=None, timeout=10):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def probe_unauth(base: str):
    return _code(f"{base}/mcp", "POST", b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
                 {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                  "User-Agent": "rose-safety-check"})


def _start_cloudflare(port: int, log) -> tuple[subprocess.Popen, str]:
    proc = subprocess.Popen(["cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                            stdout=log, stderr=subprocess.STDOUT)
    for _ in range(60):
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", (LOG_DIR / "tunnel.log").read_text())
        if m:
            return proc, m.group(0)
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("cloudflared did not report a URL (rose logs --kind tunnel)")


def _start_ngrok(port: int, log) -> tuple[subprocess.Popen, str]:
    domain = load_config().get("ngrok_domain", "").strip().lower().removeprefix("https://").rstrip("/")
    if not DOMAIN_RE.match(domain):
        raise RuntimeError("ngrok_domain is not set (your free static domain, e.g. abc-def.ngrok-free.app)")
    if not ngrok_has_authtoken():
        raise RuntimeError("ngrok has no authtoken. Run: ngrok config add-authtoken <token>")
    help_text = subprocess.run(["ngrok", "http", "--help"], capture_output=True, text=True).stdout
    flag = ["--url", f"https://{domain}"] if "--url" in help_text else ["--domain", domain]
    proc = subprocess.Popen(["ngrok", "http", f"127.0.0.1:{port}", *flag, "--log", "stdout",
                             "--log-format", "json"], stdout=log, stderr=subprocess.STDOUT)
    url = f"https://{domain}"
    for _ in range(60):  # the agent's local API lists tunnels once it's connected
        try:
            with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as r:
                if any(t.get("public_url", "").rstrip("/") == url for t in json.load(r).get("tunnels", [])):
                    return proc, url
        except Exception:
            pass
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    proc.terminate()
    tail = (LOG_DIR / "tunnel.log").read_text()[-600:]
    raise RuntimeError(f"ngrok did not start the tunnel. Log tail:\n{tail}")


def run() -> int:
    """Start the tunnel, point ROSE at it, verify auth, then stay up until the tunnel exits."""
    from rose import auth
    from rose.cli import SERVICE, _notify, _systemctl

    if not auth.auth_configured():
        print("Refusing: ROSE auth is not configured. Run: rose auth set-passphrase", file=sys.stderr)
        return 1
    missing = [s for s, ok, fix in readiness() if not ok]
    if missing:
        print("Tunnel setup incomplete: " + "; ".join(missing), file=sys.stderr)
        return 1
    port = load_config()["http_port"]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_DIR / "tunnel.log", "w")
    try:
        proc, url = (_start_ngrok if provider() == "ngrok" else _start_cloudflare)(port, log)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    def stop(*_):
        proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    # The server uses the public URL as its OAuth issuer; restart it only if the URL changed.
    if get_public_url() != url:
        set_public_url(url)
        if _systemctl("is-active", SERVICE) == "active":
            _systemctl("restart", SERVICE)
    for _ in range(40):
        if _code(f"http://127.0.0.1:{port}/.well-known/oauth-authorization-server", timeout=2) == 200:
            break
        time.sleep(0.5)

    code = None
    for _ in range(90):
        code = probe_unauth(url)
        if code == 401 or (code is not None and 200 <= code < 300):
            break
        time.sleep(1)
    if code != 401:
        print(f"SAFETY CHECK FAILED: unauthenticated request to {url}/mcp returned {code} "
              f"(expected 401). Shutting the tunnel down.", file=sys.stderr)
        proc.terminate()
        return 1

    (ROSE_HOME / "public_url.txt").write_text(f"{url}/mcp\n")
    note = "permanent URL" if is_permanent() else "new URL: update it in Claude → Connectors"
    print(f"ROSE is live (auth enforced, {note}): {url}/mcp", flush=True)
    _notify("ROSE remote access is live", f"{url}/mcp ({note})")
    rc = proc.wait()
    print(f"Tunnel exited ({rc}).", file=sys.stderr)
    return 1
