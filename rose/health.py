"""Status snapshot and "should I worry?" checks for the GUI (and anything else that wants them).

collect(probe_public=True) returns a dict; alerts are (level, title, detail) with level one of
"critical", "warn", "info", "ok".
"""

from __future__ import annotations

import contextlib
import json
import shutil
import time
from datetime import datetime, timedelta

import psutil

from rose.common import http_status, systemctl_user
from rose.common import probe_unauth as _probe
from rose.config import LOG_DIR, ROSE_HOME, get_public_url, load_config

SERVICES = ["rose", "rose-tunnel", "rose-remote"]


def probe_unauth(base: str) -> int | None:
    """HTTP status of an MCP request without credentials. 401 = auth enforced."""
    return _probe(base, timeout=6)


def service_states() -> dict[str, str]:
    """systemd state of each ROSE service ('active', 'inactive', ...)."""
    if not shutil.which("systemctl"):
        return {}
    states = {}
    for s in SERVICES:
        out = systemctl_user("is-active", s)
        states[s] = out.splitlines()[0] if out else "unknown"
    return states


def _events(days: int = 2) -> list[dict]:
    """Log entries (tool calls and auth events) from the last `days` days, oldest first."""
    entries = []
    for i in range(days - 1, -1, -1):
        p = LOG_DIR / f"{(datetime.now() - timedelta(days=i)):%Y-%m-%d}.jsonl"
        if p.exists():
            for line in p.read_text().splitlines():
                with contextlib.suppress(ValueError):
                    entries.append(json.loads(line))
    return entries


def collect(probe_public: bool = True) -> dict:
    """Snapshot for the dashboard: services, URLs, auth, activity, system, approvals, alerts."""
    from rose import auth
    from rose.tools import bridge

    cfg = load_config()
    port = cfg["http_port"]
    snap: dict = {"time": datetime.now()}
    snap["services"] = service_states()
    snap["local_up"] = http_status(f"http://127.0.0.1:{port}/.well-known/oauth-authorization-server", timeout=3) == 200
    snap["port"] = port
    url = get_public_url()
    snap["public_url"] = f"{url}/mcp" if url else ""
    snap["remote_running"] = any(snap["services"].get(s) == "active" for s in ("rose-tunnel", "rose-remote"))
    # Only probe while a tunnel runs: a remembered URL from a stopped tunnel is just dead.
    snap["public_probe"] = probe_unauth(url) if (url and probe_public and snap["remote_running"]) else None

    s = auth.load_state()
    now = time.time()
    snap["passphrase_set"] = bool(s["passphrase"])
    snap["tokens"] = [t["name"] for t in s["static_tokens"].values()]
    snap["clients"] = [
        {"id": cid, "label": s["labels"].get(cid), "name": c.get("client_name") or "?",
         "sessions": sum(1 for r in s["refresh"].values() if r["client_id"] == cid and r["expires_at"] > now)}
        for cid, c in s["clients"].items()
    ]

    events = _events(2)
    day_ago = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    today = datetime.now().strftime("%Y-%m-%d")
    calls = [e for e in events if "tool" in e]
    snap["calls_today"] = [e for e in calls if e.get("ts", "").startswith(today)]
    snap["recent_calls"] = calls[-12:][::-1]
    snap["errors_hour"] = [e for e in calls if not e.get("ok", True)
                           and e.get("ts", "") >= (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")]
    snap["remote_calls_today"] = [e for e in snap["calls_today"] if e.get("caller") not in (None, "Forge", "?")]
    snap["webcam_today"] = [e for e in snap["calls_today"] if e.get("tool") == "webcam_capture" and e.get("ok")]
    # auth events have no timestamp field in older logs; count today's file for them
    today_file = LOG_DIR / f"{today}.jsonl"
    auth_events = []
    if today_file.exists():
        for line in today_file.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if "event" in e:
                auth_events.append(e)
    mail = [e for e in auth_events if e["event"].startswith("bridge_email_")]
    snap["email_last"] = mail[-1] if mail else None
    snap["injections"] = [e for e in auth_events if e["event"] == "injection_suspected"]
    snap["approval_fails"] = [e for e in auth_events if e["event"] == "approval_wrong_passphrase"]
    snap["approvals_pending"] = [e for e in auth_events if e["event"] == "approval_requested"]
    from rose import guard
    snap["lockdown"] = guard.lockdown()
    snap["gui_grant"] = guard.grant_info()
    snap["failed_logins"] = [e for e in auth_events if e["event"] == "oauth_login_failed"]
    snap["refused_registrations"] = [e for e in auth_events if e["event"] == "oauth_register_refused"]
    snap["day_ago"] = day_ago

    vm, du = psutil.virtual_memory(), psutil.disk_usage("/")
    bat = psutil.sensors_battery()
    snap["system"] = {"cpu": psutil.cpu_percent(interval=0.3), "ram": vm.percent,
                      "ram_used_gb": round((vm.total - vm.available) / 1024**3, 1),
                      "ram_total_gb": round(vm.total / 1024**3, 1),
                      "disk": du.percent, "disk_free_gb": round(du.free / 1024**3),
                      "battery": round(bat.percent) if bat else None}
    try:
        snap["bridge"] = bridge.bridge_read(6)["messages"]
    except Exception:
        snap["bridge"] = []
    # pending approval links (newest first) and ROSE's own disk use
    pend = []
    adir = ROSE_HOME / "approvals"
    if adir.exists():
        from rose import guard
        for f in adir.glob("*.json"):
            r = guard.load_approval(f.stem)
            if r and r["status"] == "pending":
                pend.append(r)
    snap["pending_approvals"] = sorted(pend, key=lambda r: -r["created"])
    from rose import maintenance
    snap["rose_disk"] = maintenance.disk_usage()
    snap["alerts"] = alerts(snap)
    return snap


def alerts(s: dict) -> list[tuple[str, str, str]]:
    """'Should I worry?' list of (level, title, detail), most severe first."""
    a: list[tuple[str, str, str]] = []
    code = s["public_probe"]
    if s["public_url"] and code is not None and 200 <= code < 400:
        # a success/redirect without credentials means ROSE answered without auth
        a.append(("critical", "Public URL is not asking for auth",
                  f"An unauthenticated request got HTTP {code}. Stop remote access now."))
    if s["remote_running"] and not s["passphrase_set"] and not s["tokens"]:
        a.append(("critical", "Remote access is on but no auth is configured", "Stop remote access."))
    if not s["local_up"]:
        a.append(("warn", "ROSE server is not running",
                  "Claude Code and the tunnel can't reach ROSE. Click Start server."))
    if s["remote_running"] and s["public_url"] and (code is None or code >= 500):
        a.append(("warn", "Public URL is unreachable", "The tunnel may still be starting, or it dropped."))
    if s["remote_running"] and not s["public_url"]:
        a.append(("warn", "Tunnel is starting", "No public URL yet; give it a minute."))
    if s.get("lockdown"):
        a.append(("warn", "LOCKDOWN is on: ROSE is read-only",
                  "Remote and local clients can only read. Turn off with: rose lockdown off (or the button)."))
    if s.get("injections"):
        e = s["injections"][-1]
        a.append(("warn", f"{len(s['injections'])} possible prompt injection(s) today",
                  f"Latest in {e.get('tool')} output for {e.get('caller')}: {(e.get('hits') or ['?'])[0]}. "
                  "ROSE warned the model. If anything odd happened, turn on lockdown."))
    if s.get("approval_fails"):
        a.append(("warn", f"{len(s['approval_fails'])} wrong passphrase(s) on approval links today",
                  "If that wasn't you, someone has an approval link: turn on lockdown and revoke clients."))
    if s.get("gui_grant"):
        g = s["gui_grant"]
        mins = int((g["until"] - time.time()) // 60) + 1
        a.append(("info", f"{g.get('caller', '?')} can act without asking for {mins} more min",
                  "You approved an action, which opened this window; it expires by itself. "
                  "End it now with: rose grant end (lockdown also ends it)."))
    if s.get("pending_approvals"):
        a.append(("warn", f"{len(s['pending_approvals'])} approval(s) waiting for you",
                  "Approve or deny them in the Approvals card (or from the link on your phone)."))
    last = s.get("email_last")
    if last and last["event"] == "bridge_email_failed":
        a.append(("warn", "Bridge emails are failing", last.get("error", "")[:160]))
    if s["failed_logins"]:
        a.append(("warn", f"{len(s['failed_logins'])} wrong passphrase attempt(s) today",
                  "If that wasn't you, someone has your connector URL. Revoke clients and restart the tunnel."))
    if s["refused_registrations"]:
        a.append(("info", f"{len(s['refused_registrations'])} unknown app(s) tried to register today",
                  "ROSE refused them (redirect not on the allowlist). Nothing to do."))
    unlabeled = [c for c in s["clients"] if not c["label"]]
    if unlabeled:
        a.append(("warn", f"{len(unlabeled)} connected client(s) without a name",
                  "If you don't recognise them, revoke them. Name yours with: rose auth label <id> <name>"))
    if s["errors_hour"]:
        a.append(("info", f"{len(s['errors_hour'])} tool error(s) in the last hour",
                  "Usually refusals (confirm needed, path not allowed). See Activity."))
    if s["webcam_today"]:
        a.append(("info", f"Webcam used {len(s['webcam_today'])} time(s) today",
                  ", ".join(f"{e['ts'][11:16]} by {e.get('caller', '?')}" for e in s["webcam_today"][-5:])))
    sy = s["system"]
    if sy["ram"] >= 90:
        a.append(("warn", f"RAM at {sy['ram']:.0f}%", "Close something heavy (browser tabs, VMs)."))
    if sy["disk"] >= 90:
        a.append(("warn", f"Disk at {sy['disk']:.0f}%", f"Only {sy['disk_free_gb']} GB free."))
    if sy["battery"] is not None and sy["battery"] <= 5:
        a.append(("info", f"Battery reads {sy['battery']}%",
                  "It's been at 0% and 'Not charging' all day: likely a worn battery or a charge limit."))
    if not s["remote_running"]:
        a.append(("info", "Remote access is off", "claude.ai and your phone can't reach ROSE right now."))
    if not any(lvl in ("critical", "warn") for lvl, _, _ in a):
        a.insert(0, ("ok", "Nothing to worry about", "ROSE is up and remote access requires auth."))
    order = {"critical": 0, "warn": 1, "ok": 2, "info": 3}
    return sorted(a, key=lambda x: order[x[0]])
