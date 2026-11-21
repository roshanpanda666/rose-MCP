"""Bridge: a shared markdown file for messages between your Claude accounts and Forge (Claude Code).

Each message is appended to BRIDGE.md as a markdown section. The sender is taken from how the
caller authenticated to ROSE (local stdio = Forge; each OAuth client/static token has a label set
with `python -m rose.auth label`), so a model can't post under another account's name.
"""

from __future__ import annotations

import fcntl
import re
from datetime import datetime
from pathlib import Path

from rose.common import ToolError
from rose.config import ROSE_HOME, load_config

HEADER = """# ROSE Bridge

Shared notes between the user's Claude accounts and Forge (Claude Code), relayed by ROSE.
Newest messages are at the bottom. Use the ROSE tools `bridge_read` and `bridge_post`
rather than editing by hand while a conversation is using it.

"""
_ENTRY = re.compile(r"^### (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) · (.+?) → (.+?)$", re.MULTILINE)
MAX_MESSAGE = 20_000


def bridge_path() -> Path:
    p = load_config().get("bridge_path") or str(ROSE_HOME / "bridge" / "BRIDGE.md")
    return Path(p).expanduser()


def caller() -> str:
    """Who is calling, from the auth context: 'Forge' locally, else the client's label."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
        tok = get_access_token()
    except Exception:
        tok = None
    if tok is None:
        return "Forge"
    from rose import auth
    return auth.client_label(tok.client_id)


def _parse(text: str) -> list[dict]:
    matches = list(_ENTRY.finditer(text))
    out = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append({"time": m.group(1), "from": m.group(2), "to": m.group(3),
                    "message": text[m.end():end].strip()})
    return out


def bridge_read(last: int = 10, since: str = "", only_for_me: bool = False) -> dict:
    """Bridge messages: last N (max 100) or after since ('YYYY-MM-DD HH:MM:SS'); only_for_me filters."""
    p = bridge_path()
    me = caller()
    entries = _parse(p.read_text()) if p.exists() else []
    if since:
        entries = [e for e in entries if e["time"] > since]
    if only_for_me:
        entries = [e for e in entries if e["to"] in ("all", me)]
    last = max(1, min(last, 100))
    return {"you_are": me, "file": str(p), "total_shown": min(len(entries), last),
            "messages": entries[-last:]}


def bridge_post(message: str, to: str = "all") -> dict:
    """Post to the bridge (to: 'all' or a name). If result has email_copy, send it with Gmail now."""
    from rose import mailer

    result: dict = {"posted": post_as(caller(), message, to, email=False)}
    entry = _parse((bridge_path()).read_text())[-1]
    if mailer.recipients() and mailer.load_settings():
        mailer.send_bridge_copy(entry)  # ROSE has its own sender (rose email setup)
        result["email"] = "ROSE is emailing a copy."
    elif mailer.recipients():
        result["email_copy"] = mailer.build_email(entry)
        result["instruction"] = ("Send email_copy now using your Gmail tool's send-message action "
                                 "(send it, don't just draft it), exactly as given: same to, subject "
                                 "and body. If you have no Gmail tool, tell the user the copy wasn't sent.")
    return result


def post_as(sender: str, message: str, to: str = "all", email: bool = True) -> str:
    """Post with an explicit sender. Not exposed as a tool (used by the local GUI as 'Roshan').
    email=True asks ROSE's own sender (if set up) to email a copy."""
    message = message.strip()
    if not message:
        raise ToolError("Message is empty.")
    if len(message) > MAX_MESSAGE:
        raise ToolError(f"Message too long ({len(message)} chars, max {MAX_MESSAGE}). Split it up.")
    to = re.sub(r"[\r\n→]", " ", to).strip()[:60] or "all"
    # Keep the file parseable: a line that looks like an entry header can't appear inside a message.
    message = re.sub(r"^### ", "\\### ", message, flags=re.MULTILINE)
    p = bridge_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    me = re.sub(r"[\r\n→·]", " ", sender).strip()[:60] or "?"
    with open(p, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)  # several clients may post at once
        f.seek(0, 2)
        if f.tell() == 0:
            f.write(HEADER)
        f.write(f"### {stamp} · {me} → {to}\n\n{message}\n\n")
        fcntl.flock(f, fcntl.LOCK_UN)
    if email:
        try:
            from rose import mailer
            mailer.send_bridge_copy({"time": stamp, "from": me, "to": to, "message": message})
        except Exception:
            pass  # email is best-effort; the bridge file is the source of truth
    return f"Posted to the bridge as {me} → {to} at {stamp}."
