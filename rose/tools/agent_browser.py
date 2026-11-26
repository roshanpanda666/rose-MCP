"""Vercel Labs' agent-browser CLI as ROSE tools: drive a Chromium browser through accessibility-tree
snapshots with @eN element refs instead of screenshots.

Off by default (agent_browser_enabled = false); the existing browser_* tools are untouched.

What it attaches to (agent_browser_target):
* "brave" (default) / "chrome" / "chromium": the user's own, signed-in, already-running browser. It
  must share remote debugging: open brave://inspect/#remote-debugging and turn on "Allow remote
  debugging for this browser instance". The browser then listens on 127.0.0.1 only, writes the
  DevToolsActivePort file this module reads, and asks the user to allow each new connection.
  (Chromium 136+ ignores --remote-debugging-port on the default profile, so this is the only way in.)
* "rose": ROSE's own unsigned browser (browser_start; already bound to 127.0.0.1).
* a port number: any browser already started with --remote-debugging-port on this machine.

Safety rules here:
* Loopback only. ROSE only ever connects to 127.0.0.1, and refuses a port that also listens on any
  non-loopback address (checked in /proc/net/tcp{,6}), so a debugging port exposed to the network is
  never used.
* Never launches a browser. Every call attaches (--cdp, or --auto-connect for a browser shared through
  brave://inspect, which waits for the user's Allow); a call without either would quietly start its own
  Chrome. The target's DevToolsActivePort and loopback-only listener are checked first.
* Own tab only. agent_browser_open opens a new tab and pins the session to it (--pin-tab), so
  commands never fall through to the user's other tabs; close closes only the tabs ROSE opened and
  detaches (the user's browser keeps running).
* Data after "--": text and keys always follow the CLI's "--" boundary, so page or user text that
  looks like a flag (e.g. "--cdp ws://...") stays data. No eval tool is exposed.
* Submit/Enter need confirm=true (clicking a submit/send-like control, pressing Enter or Space).
* Refs are checked against the latest snapshot; stale refs fail with "take a new snapshot", and an
  action is never retried on a different element.
* Snapshot/read output goes through guard's untrusted-content label and injection scanner
  (UNTRUSTED_TOOLS); error text from the page is scanned too.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from rose import guard
from rose.common import ToolError
from rose.config import ROSE_HOME, load_config

SESSION = "rose"
STATE_FILE = ROSE_HOME / "agent_browser.json"  # tabs ROSE opened + the last snapshot's refs
USER_DATA_DIRS = {
    "brave": Path.home() / ".config" / "BraveSoftware" / "Brave-Browser",
    "chrome": Path.home() / ".config" / "google-chrome",
    "chromium": Path.home() / ".config" / "chromium",
}
TOOL_NAMES = ("agent_browser_status", "agent_browser_open", "agent_browser_snapshot", "agent_browser_click",
              "agent_browser_fill", "agent_browser_press", "agent_browser_read", "agent_browser_wait",
              "agent_browser_close")

REF_RE = re.compile(r"^@?(e\d{1,6})$")
# Accessible names that usually commit something: clicking one needs confirm=true.
SUBMIT_NAME_RE = re.compile(
    r"\b(send|submit|post|publish|reply|tweet|share|pay|buy|order|checkout|purchase|subscribe|confirm|"
    r"delete|remove|transfer|sign ?up|register|place order|save|apply|approve|merge|deploy)\b", re.IGNORECASE)
# Fixed page script (no inputs): is the focused element a form-submitting control?
SUBMIT_PROBE = ("(() => { const e = document.activeElement; if (!e || e === document.body) return null;"
                " const t = String(e.type || '').toLowerCase(); const inForm = !!(e.form || e.closest('form'));"
                " return {tag: e.tagName, type: t, inForm: inForm}; })()")
STALE_PATTERNS = ("unknown ref", "could not locate element", "no longer loaded", "document changed",
                  "ambiguous", "run snapshot again")
NAMED_KEYS = {"Enter", "Tab", "Escape", "Backspace", "Delete", "Space", "ArrowUp", "ArrowDown", "ArrowLeft",
              "ArrowRight", "Home", "End", "PageUp", "PageDown", *(f"F{i}" for i in range(1, 13))}
MODIFIERS = {"Control", "Alt", "Shift", "Meta"}
CONFIRM_KEYS = {"Enter", "Space"}  # both activate a focused button / submit a form
LOAD_STATES = ("load", "domcontentloaded", "networkidle")


# ---------------- plumbing ----------------

def enabled() -> bool:
    return bool(load_config().get("agent_browser_enabled", False))


def _require_enabled() -> None:
    if not enabled():
        raise ToolError("agent-browser tools are off. Set agent_browser_enabled = true in ROSE's config.")


def _binary() -> str:
    b = str(load_config().get("agent_browser_binary") or "").strip()
    path = shutil.which(os.path.expanduser(b)) if b else shutil.which("agent-browser")
    if not path:
        raise ToolError("agent-browser isn't installed. Install it with `npm install -g agent-browser` "
                        "(or set agent_browser_binary). No browser download is needed: ROSE attaches to yours.")
    return path


def _listeners(port: int) -> list[str]:
    """Hex local addresses of sockets LISTENing on `port` (from /proc/net/tcp and tcp6)."""
    out = []
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(f).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 4 or parts[3] != "0A":  # 0A = LISTEN
                continue
            addr, _, p = parts[1].rpartition(":")
            if int(p, 16) == port:
                out.append(addr.upper())
    return out


_LOOPBACK = {"0100007F", "00000000000000000000000001000000", "0000000000000000FFFF00000100007F"}


def _is_loopback(hex_addr: str) -> bool:
    if len(hex_addr) == 8:  # IPv4, little-endian: 127.x.x.x has 7F as its last byte
        return hex_addr.endswith("7F")
    return hex_addr in _LOOPBACK or (hex_addr.startswith("0000000000000000FFFF0000") and hex_addr.endswith("7F"))


def check_loopback_only(port: int) -> None:
    """Raise unless something listens on `port` and every listener is a loopback address."""
    addrs = _listeners(port)
    if not addrs:
        raise ToolError(f"Nothing is listening on 127.0.0.1:{port}. Is the browser running with remote debugging on?")
    exposed = [a for a in addrs if not _is_loopback(a)]
    if exposed:
        raise ToolError(f"Refusing: the debugging port {port} is reachable beyond this machine (not bound to "
                        "127.0.0.1 only). Restart that browser with --remote-debugging-address=127.0.0.1.")


def _target() -> str:
    return str(load_config().get("agent_browser_target", "brave")).strip().lower() or "brave"


def endpoint() -> str:
    """The CDP endpoint to attach to, always on 127.0.0.1, after the loopback-only check."""
    target = _target()
    path = ""
    if target == "rose":
        from rose.tools import browser
        port = browser._port()
    elif target.isdigit():
        port = int(target)
    elif target in USER_DATA_DIRS:
        f = USER_DATA_DIRS[target] / "DevToolsActivePort"
        try:
            lines = f.read_text().split()
            port = int(lines[0])
            path = lines[1] if len(lines) > 1 and lines[1].startswith("/devtools/browser/") else ""
        except (OSError, ValueError, IndexError):
            raise ToolError(
                f"{target.title()} isn't sharing remote debugging. In {target.title()} open "
                f"{'brave' if target == 'brave' else 'chrome'}://inspect/#remote-debugging and turn on "
                "'Allow remote debugging for this browser instance' (it stays on 127.0.0.1), then try again; "
                "the browser asks you to allow the connection. Or set agent_browser_target = \"rose\" for "
                "ROSE's own (unsigned) browser.") from None
    else:
        raise ToolError(f"agent_browser_target must be brave, chrome, chromium, rose or a port number, not {target!r}.")
    if not 0 < port < 65536:
        raise ToolError(f"Bad debugging port {port}.")
    check_loopback_only(port)
    return f"ws://127.0.0.1:{port}{path}" if path else f"http://127.0.0.1:{port}"


def attach_flags() -> list[str]:
    """How to attach, after endpoint()'s loopback check. A browser shared through
    brave://inspect/#remote-debugging asks the user to Allow each new connection; --cdp gives up before
    they can click, while --auto-connect waits for the answer (and the daemon then reuses the approved
    connection). --auto-connect finds the browser by its DevToolsActivePort file, so it is only used
    when exactly one browser shares one; otherwise ROSE pins the endpoint with --cdp."""
    ep = endpoint()
    target = _target()
    if target in USER_DATA_DIRS:
        sharing = [n for n, d in USER_DATA_DIRS.items() if (d / "DevToolsActivePort").is_file()]
        if sharing == [target]:
            return ["--auto-connect"]
    return ["--cdp", ep]


def _env(timeout_ms: int) -> dict:
    # Drop inherited AGENT_BROWSER_* settings (an AGENT_BROWSER_CDP could point anywhere).
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_BROWSER_")}
    env["AGENT_BROWSER_DEFAULT_TIMEOUT"] = str(timeout_ms)
    return env


def _safe_error(msg: str) -> str:
    msg = " ".join(str(msg or "agent-browser failed").split())[:300]
    if guard.scan(msg):  # page text can end up in error messages: don't hand an injection back as an error
        return "agent-browser reported an error whose text looked like a prompt injection (withheld)."
    return msg


def _run(args: list[str], *, attach: bool = True, timeout: int = 30, wait_ms: int = 10000,
         extra_flags: tuple[str, ...] = ()) -> dict:
    """Run one agent-browser command (JSON mode); return its data dict or raise ToolError."""
    cmd = [_binary(), "--json", "--session", SESSION, *extra_flags]
    if attach:
        cmd += attach_flags()
    cmd += args
    ROSE_HOME.mkdir(parents=True, exist_ok=True)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_env(wait_ms),
                           cwd=str(ROSE_HOME))  # not the caller's cwd: no stray ./agent-browser.json
    except subprocess.TimeoutExpired:
        raise ToolError(f"agent-browser timed out after {timeout}s. If the browser is asking to allow remote "
                        "debugging, click Allow there and try again.") from None
    res = None
    for line in reversed(p.stdout.strip().splitlines()):
        try:
            res = json.loads(line)
            break
        except ValueError:
            continue
    if not isinstance(res, dict):
        raise ToolError(_safe_error(p.stderr or p.stdout or f"agent-browser exited with {p.returncode}"))
    if not res.get("success"):
        err = str(res.get("error") or "")
        low = err.lower()
        if "tab_gone" in low:
            _forget_tabs()
            raise ToolError("ROSE's browser tab was closed. Call agent_browser_open again.")
        if "remote-debugging approval" in low or ("waiting for" in low and "approval" in low):
            raise ToolError("The browser is asking to allow remote debugging: click Allow there, then try again.")
        if any(s in low for s in STALE_PATTERNS):
            raise ToolError(f"Stale element ref ({_safe_error(err)}). The page changed: call agent_browser_snapshot "
                            "and use the new refs (ref numbers change between snapshots).")
        raise ToolError(_safe_error(err))
    data = res.get("data") or {}
    return data if isinstance(data, dict) else {"value": data}


def _load_state() -> dict:
    try:
        s = json.loads(STATE_FILE.read_text())
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(s: dict) -> None:
    ROSE_HOME.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(s))


def _forget_tabs() -> None:
    s = _load_state()
    s["tabs"], s["refs"] = [], {}
    _save_state(s)


def _ref(ref: str) -> tuple[str, dict]:
    """'@e3' / 'e3' -> ('@e3', {role, name}) from the latest snapshot; stale/unknown -> ToolError."""
    m = REF_RE.match(str(ref).strip())
    if not m:
        raise ToolError(f"{ref!r} isn't an element ref. Use a ref like @e3 from agent_browser_snapshot.")
    refs = _load_state().get("refs") or {}
    if m.group(1) not in refs:
        raise ToolError(f"Ref @{m.group(1)} isn't in the latest snapshot (stale or never issued). "
                        "Call agent_browser_snapshot and use its refs.")
    return "@" + m.group(1), refs[m.group(1)]


def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + f"\n… [cut at {n} of {len(text)} chars]"


# ---------------- tools ----------------

def agent_browser_status() -> dict:
    """agent-browser setup: installed?, which browser it attaches to, whether that port is loopback-only, ROSE's tabs."""
    out: dict = {"enabled": enabled(), "target": _target()}
    try:
        b = _binary()
        out["binary"] = b
        v = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=10, env=_env(10000))
        out["version"] = v.stdout.strip()
    except (ToolError, OSError, subprocess.TimeoutExpired) as e:
        out["binary_error"] = str(e)
    try:
        out["endpoint"] = endpoint()
        out["attach"] = attach_flags()[0]
        out["reachable"] = True
    except ToolError as e:
        out["reachable"] = False
        out["endpoint_error"] = str(e)
    out["rose_tabs"] = len(_load_state().get("tabs") or [])
    return out


def agent_browser_open(url: str, new_tab: bool = True) -> dict:
    """Open a URL in the user's signed-in browser via agent-browser (new ROSE tab by default). Then agent_browser_snapshot."""
    _require_enabled()
    from rose.tools.browser import _check_url
    url = _check_url(url)
    s = _load_state()
    tabs = s.get("tabs") or []
    if new_tab or not tabs:  # never navigate one of the user's own tabs
        try:
            d = _run(["tab", "new", "--", url], timeout=60, extra_flags=("--pin-tab",))
        except ToolError:
            # The first call can time out while the user is still clicking Allow, after the tab was
            # already opened. Adopt such a tab so close still cleans it up, then report the failure.
            _adopt_tabs(url, tabs)
            s["tabs"] = tabs
            _save_state(s)
            raise
        if d.get("targetId"):
            tabs.append(d["targetId"])
    else:
        d = _run(["open", "--", url], timeout=60)
    s["tabs"], s["refs"] = tabs, {}
    _save_state(s)
    return {"opened": d.get("url") or url, "title": d.get("title", ""),
            "next": "agent_browser_snapshot to see the page and get element refs."}


def _adopt_tabs(url: str, tabs: list) -> None:
    try:
        listed = _run(["tab", "list"]).get("tabs") or []
    except ToolError:
        return
    for t in listed:
        if isinstance(t, dict) and t.get("url", "").rstrip("/") == url.rstrip("/") and t.get("targetId") not in tabs:
            tabs.append(t["targetId"])


def agent_browser_snapshot(interactive: bool = True, max_chars: int = 12000) -> dict:
    """Accessibility-tree snapshot of ROSE's tab with @eN refs for click/fill. interactive=false also shows text. Re-run after page changes."""
    _require_enabled()
    args = ["snapshot", "-c"] + (["-i"] if interactive else [])
    d = _run(args)
    refs = {k: {"role": str(v.get("role", "")), "name": str(v.get("name", ""))[:200]}
            for k, v in (d.get("refs") or {}).items() if isinstance(v, dict)}
    s = _load_state()
    s["refs"] = refs
    _save_state(s)
    return {"origin": d.get("origin", ""), "snapshot": _cap(str(d.get("snapshot", "")), max(1000, min(max_chars, 50000)))}


def _looks_like_submit(ref: str, info: dict) -> bool:
    if info.get("role") in ("button", "link", "menuitem") and SUBMIT_NAME_RE.search(info.get("name", "")):
        return True
    if info.get("role") not in ("button", "menuitem", ""):
        return False
    try:  # does it submit a form? (focus, then a fixed probe script; no page input goes into code)
        _run(["focus", ref])
        r = _run(["eval", SUBMIT_PROBE]).get("result") or {}
    except ToolError:
        return False
    if not isinstance(r, dict) or not r.get("inForm"):
        return False
    return (r.get("tag") == "BUTTON" and r.get("type") == "submit") or \
           (r.get("tag") == "INPUT" and r.get("type") in ("submit", "image"))


def agent_browser_click(ref: str, confirm: bool = False) -> str:
    """Click an element by snapshot ref (@e3). Submit/send-like buttons need confirm=true: ask the user first."""
    _require_enabled()
    r, info = _ref(ref)
    label = f"{info.get('role', '')} {info.get('name', '')!r}".strip()
    if not confirm and _looks_like_submit(r, info):
        raise ToolError(f"Clicking {label} may submit or send something. Ask the user, then call again with confirm=true.")
    _run(["click", r])
    s = _load_state()
    s["refs"] = {}  # the page may have changed: force a fresh snapshot before the next ref action
    _save_state(s)
    return f"Clicked {label} ({r}). Call agent_browser_snapshot before using refs again."


def agent_browser_fill(ref: str, text: str) -> str:
    """Clear a field (by snapshot ref) and type text into it. Never submits. Reads back."""
    _require_enabled()
    if len(text) > 10000:
        raise ToolError("Text too long.")
    r, info = _ref(ref)
    _run(["fill", r, "--", text])
    msg = f"Filled {info.get('name', '')!r} ({r}) with {len(text)} characters. Not submitted."
    want = " ".join(text.split())
    seen = False
    for what in ("value", "text"):  # inputs have a value; contenteditable editors only have text
        try:
            d = _run(["get", what, r])
        except ToolError:
            continue
        v = d.get(what, d.get("value"))
        if v is None:
            continue
        seen = True
        if " ".join(str(v).split()) == want:
            return msg + " Read-back OK."
    if not seen:
        return msg + " Read-back unavailable; check with agent_browser_snapshot."
    return msg + " READ-BACK MISMATCH: the field doesn't hold that text. Snapshot and retry."


def agent_browser_press(key: str, confirm: bool = False) -> str:
    """Press a key in ROSE's tab: Tab, Escape, Arrow*, Backspace, Control+a... Enter/Space need confirm=true."""
    _require_enabled()
    parts = [p.strip() for p in str(key).split("+")]
    named = {k.lower(): k for k in NAMED_KEYS | MODIFIERS}
    *mods, base = [named.get(p.lower(), p) for p in parts]
    ok_base = base in NAMED_KEYS or re.fullmatch(r"[A-Za-z0-9]", base or "")
    if not ok_base or any(m not in MODIFIERS for m in mods):
        raise ToolError(f"Key must be one of {sorted(NAMED_KEYS)} or a letter/digit, optionally with "
                        "Control+/Alt+/Shift+/Meta+.")
    if base in CONFIRM_KEYS and not confirm:
        raise ToolError(f"{base} may send a message or submit a form. Ask the user, then call again with confirm=true.")
    combo = "+".join([*mods, base])
    _run(["press", "--", combo])
    if base in CONFIRM_KEYS:
        s = _load_state()
        s["refs"] = {}
        _save_state(s)
    return f"Pressed {combo}."


def agent_browser_read(max_chars: int = 8000) -> dict:
    """Readable text of ROSE's agent-browser tab (rendered page)."""
    _require_enabled()
    d = _run(["read"])
    return {"url": d.get("finalUrl") or d.get("url", ""),
            "text": _cap(str(d.get("content", "")), max(500, min(max_chars, 50000)))}


def agent_browser_wait(text: str = "", load: str = "", ms: int = 0, timeout_s: int = 10) -> str:
    """Wait in ROSE's tab for one of: text to appear, a load state (load/domcontentloaded/networkidle), or ms."""
    _require_enabled()
    given = [x for x in (text, load, ms) if x]
    if len(given) != 1:
        raise ToolError("Give exactly one of text, load or ms.")
    timeout_ms = max(1, min(int(timeout_s), 30)) * 1000
    if text:
        if text.startswith("-"):
            raise ToolError("Wait text can't start with '-'; leave the dash off.")
        args = ["wait", "--text", text[:200]]
    elif load:
        if load not in LOAD_STATES:
            raise ToolError(f"load must be one of {LOAD_STATES}.")
        args = ["wait", "--load", load]
    else:
        args = ["wait", str(max(1, min(int(ms), 30000)))]
    _run(args, timeout=timeout_ms // 1000 + 15, wait_ms=timeout_ms)
    return "Done: " + (f"text {text[:60]!r} is on the page." if text else f"page reached {load}." if load
                       else f"waited {args[1]} ms.")


def agent_browser_close(detach: bool = False) -> str:
    """Close the tabs ROSE opened with agent_browser_open. detach=true also drops the connection (next use asks Allow again)."""
    _require_enabled()
    tabs = _load_state().get("tabs") or []
    closed = 0
    if tabs:
        try:
            endpoint()
            for t in tabs:
                try:
                    _run(["tab", "close", "--", t])
                    closed += 1
                except ToolError:
                    pass  # already closed by the user
        except ToolError:
            pass  # browser gone: nothing to close
    if detach:
        try:
            _run(["close"], attach=False)  # attached browsers are not owned: this only detaches
        except ToolError:
            pass
    _forget_tabs()
    # Staying attached keeps the user's Allow: a browser shared via brave://inspect asks again on every
    # new connection. The agent-browser daemon drops it by itself after an hour idle.
    return (f"Closed {closed} ROSE tab(s)" + (" and detached" if detach else "; still connected (no new Allow prompt next time)")
            + ". Your browser keeps running.")
