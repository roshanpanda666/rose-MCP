"""Pair the phone by QR code (Android 11+ "Wireless debugging → Pair device with QR code").

We show a QR with  WIFI:T:ADB;S:<name>;P:<password>;;  The phone scans it and announces a pairing service
called <name> over mDNS (_adb-tls-pairing._tcp). We find it with avahi-browse, run `adb pair ip:port password`,
then connect to the phone's _adb-tls-connect._tcp service on the same IP. Needs the phone and the laptop on
the same network (mDNS doesn't cross Tailscale).
"""

from __future__ import annotations

import secrets
import string
import subprocess
import threading
import time

_job: dict = {"state": "idle", "message": "", "name": "", "address": "", "gen": 0}
_lock = threading.Lock()
TIMEOUT = 150  # s to scan and pair


def payload(name: str, password: str) -> str:
    return f"WIFI:T:ADB;S:{name};P:{password};;"


def qr_svg(text: str) -> str:
    """The QR as SVG (qrencode)."""
    r = subprocess.run(["qrencode", "-t", "SVG", "-m", "1", "-o", "-", text], capture_output=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError("qrencode failed (sudo apt install qrencode)")
    return r.stdout.decode()


def parse_avahi(out: str, service: str) -> list[dict]:
    """Resolved IPv4 entries of `service` from `avahi-browse -rpt` output: [{name, ip, port}]."""
    found = []
    for line in out.splitlines():
        f = line.split(";")
        if len(f) >= 9 and f[0] == "=" and f[2] == "IPv4" and f[4] == service:
            try:
                found.append({"name": f[3].replace("\\032", " "), "ip": f[7], "port": int(f[8])})
            except ValueError:
                continue
    return found


def _browse(service: str) -> list[dict]:
    try:
        r = subprocess.run(["avahi-browse", "-rpt", service], capture_output=True, text=True, timeout=8)
        return parse_avahi(r.stdout, service)
    except (OSError, subprocess.TimeoutExpired):
        return []


def find_port(ip: str, skip: set[int] | None = None, lo: int = 30000, hi: int = 50000) -> int | None:
    """The phone's wireless-debugging connect port when it isn't announced over mDNS: first open port in lo..hi."""
    import concurrent.futures as cf
    import socket

    def probe(port: int) -> int | None:
        if skip and port in skip:
            return None
        with socket.socket() as sock:
            sock.settimeout(0.4)
            try:
                sock.connect((ip, port))
                return port
            except OSError:
                return None

    with cf.ThreadPoolExecutor(400) as ex:
        return next((p for p in ex.map(probe, range(lo, hi)) if p), None)


def _set(gen: int, **kw) -> bool:
    with _lock:
        if _job["gen"] != gen:
            return False
        _job.update(kw)
        return True


def _run(gen: int, name: str, password: str) -> None:
    from rose.tools import phone

    end = time.time() + TIMEOUT
    pair = None
    while time.time() < end and _job["gen"] == gen:
        pair = next((s for s in _browse("_adb-tls-pairing._tcp") if s["name"] == name), None)
        if pair:
            break
        time.sleep(1)
    if _job["gen"] != gen:
        return
    if not pair:
        _set(gen, state="error", message="No phone scanned the code in time. Same Wi-Fi as the laptop? Try again.")
        return
    _set(gen, state="pairing", message=f"Phone found at {pair['ip']}, pairing…")
    try:
        out = phone._adb("pair", f"{pair['ip']}:{pair['port']}", password, timeout=30)
    except Exception as e:
        _set(gen, state="error", message=f"Pairing failed: {e}")
        return
    if "Successfully paired" not in str(out):
        _set(gen, state="error", message=f"Pairing failed: {str(out)[-200:]}")
        return
    _set(gen, state="connecting", message="Paired. Connecting…")
    for _ in range(4):
        if _job["gen"] != gen:
            return
        conn = next((s for s in _browse("_adb-tls-connect._tcp") if s["ip"] == pair["ip"]), None)
        if conn:
            addr = f"{conn['ip']}:{conn['port']}"
            try:
                msg = phone.phone_connect(addr)
                _set(gen, state="done", message=msg, address=addr)
            except Exception as e:
                _set(gen, state="error", message=f"Paired, but connecting failed: {e}")
            return
        time.sleep(1.5)
    if _job["gen"] != gen:
        return
    _set(gen, message="Paired. Looking for the phone's debugging port…")
    port = find_port(pair["ip"], skip={pair["port"]})
    if port and _job["gen"] == gen:
        addr = f"{pair['ip']}:{port}"
        try:
            _set(gen, state="done", message=phone.phone_connect(addr), address=addr)
            return
        except Exception as e:
            _set(gen, state="error", message=f"Paired, but connecting to {addr} failed: {e}")
            return
    _set(gen, state="error", message="Paired, but couldn't find the phone's debugging port: type the IP:port "
                                     "Wireless debugging shows and tap Connect (no code needed now).")


def start() -> dict:
    """New code + QR; pairing runs in the background. Poll status()."""
    alphabet = string.ascii_letters + string.digits
    name = "rose-" + "".join(secrets.choice(alphabet) for _ in range(8))
    password = "".join(secrets.choice(alphabet) for _ in range(10))
    svg = qr_svg(payload(name, password))
    with _lock:
        _job.update(state="waiting", message="Scan the code on your phone.", name=name, address="", gen=_job["gen"] + 1)
        gen = _job["gen"]
    threading.Thread(target=_run, args=(gen, name, password), daemon=True).start()
    return {"svg": svg, "name": name, "timeout": TIMEOUT}


def status() -> dict:
    with _lock:
        return {k: _job[k] for k in ("state", "message", "address")}


def cancel() -> str:
    with _lock:
        _job.update(state="idle", message="", gen=_job["gen"] + 1)
    return "OK"
