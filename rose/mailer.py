"""Email copies of bridge posts (Gmail SMTP with an app password).

Recipients live in config.toml (`bridge_email_to`). The sending account and its app password live in
~/rose/auth/email.json (mode 0600), set with `rose email setup`. Sending runs in a background thread so
a slow or failing mail server never blocks a tool call; the outcome is logged and shown in the GUI.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from rose.common import _log
from rose.config import ROSE_HOME, load_config

EMAIL_PATH = ROSE_HOME / "auth" / "email.json"
MAX_PER_HOUR = 40  # guard against a runaway loop flooding your inbox
_sent: list[float] = []
_lock = threading.Lock()


def load_settings() -> dict | None:
    try:
        return json.loads(EMAIL_PATH.read_text())
    except (OSError, ValueError):
        return None


def save_settings(s: dict) -> None:
    EMAIL_PATH.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(EMAIL_PATH.parent, 0o700)
    fd = os.open(EMAIL_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(s, f, indent=2)


def recipients() -> list[str]:
    return [r for r in load_config().get("bridge_email_to", []) if "@" in r]


def _send(subject: str, body: str, to: list[str]) -> None:
    s = load_settings()
    if not s:
        raise RuntimeError("email not set up (run: rose email setup)")
    msg = EmailMessage()
    msg["From"] = formataddr(("ROSE", s["username"]))
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject.replace("\r", " ").replace("\n", " ")[:200]
    msg["Message-ID"] = make_msgid(domain="rose.local")
    msg.set_content(body)
    host, port = s.get("smtp_host", "smtp.gmail.com"), int(s.get("smtp_port", 587))
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if s.get("starttls", True):
            smtp.starttls(context=ssl.create_default_context())
        if s.get("password"):
            smtp.login(s["username"], s["password"])
        smtp.send_message(msg)


def build_email(entry: dict) -> dict:
    """The email for one bridge entry, as {to, subject, body} (also used when Claude sends it)."""
    msg = entry["message"].strip()
    first = msg.splitlines()[0][:80] if msg else ""
    return {
        "to": recipients(),
        "subject": f"[ROSE bridge] {entry['from']} → {entry['to']}: {first}",
        "body": (f"{entry['from']} → {entry['to']}   ({entry['time']})\n\n{msg}\n\n"
                 f"--\nPosted on the ROSE bridge. Reply on the bridge (ROSE dashboard, or ask Claude), "
                 f"not by email."),
    }


def send_bridge_copy(entry: dict) -> None:
    """Fire-and-forget email of one bridge entry to the configured recipients."""
    to = recipients()
    if not to or not load_config().get("bridge_email_enabled", True) or not load_settings():
        return

    def work():
        with _lock:
            now = time.time()
            _sent[:] = [t for t in _sent if now - t < 3600]
            if len(_sent) >= MAX_PER_HOUR:
                _log({"event": "bridge_email_skipped", "reason": "hourly limit", "time": entry["time"]})
                return
            _sent.append(now)
        e = build_email(entry)
        subject, body = e["subject"], e["body"]
        try:
            _send(subject, body, to)
            _log({"event": "bridge_email_sent", "time": entry["time"], "to": len(to)})
        except Exception as e:
            _log({"event": "bridge_email_failed", "time": entry["time"], "error": f"{type(e).__name__}: {e}"})

    threading.Thread(target=work, daemon=True).start()


def setup_cli(argv: list[str]) -> int:
    import getpass

    cmd = argv[0] if argv else "status"
    if cmd == "setup":
        print("ROSE sends bridge emails from a Gmail account using an *app password*\n"
              "(Google Account → Security → 2-Step Verification → App passwords).\n")
        user = input("Sending Gmail address: ").strip()
        pw = getpass.getpass("App password (16 letters, spaces ok): ").replace(" ", "")
        save_settings({"username": user, "password": pw, "smtp_host": "smtp.gmail.com", "smtp_port": 587,
                       "starttls": True})
        print(f"Saved to {EMAIL_PATH} (only you can read it). Recipients: {', '.join(recipients()) or 'none'}")
        print("Send a test with: rose email test")
    elif cmd == "test":
        to = recipients()
        if not to:
            print("No recipients: set bridge_email_to in the config (rose config).")
            return 1
        try:
            _send("[ROSE] test email", "If you can read this, ROSE bridge emails work.", to)
        except Exception as e:
            print(f"Failed: {type(e).__name__}: {e}")
            return 1
        print(f"Sent to {', '.join(to)}")
    elif cmd == "off":
        EMAIL_PATH.unlink(missing_ok=True)
        print("Email credentials removed; bridge emails are off.")
    else:
        s = load_settings()
        print(f"Sender:     {s['username'] if s else 'not set up (rose email setup)'}")
        print(f"Recipients: {', '.join(recipients()) or 'none'}")
    return 0
