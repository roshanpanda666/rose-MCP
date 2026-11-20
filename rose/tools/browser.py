"""Browser control over the Chrome DevTools Protocol (CDP), in a dedicated ROSE browser profile.

ROSE starts Brave (or Chrome/Chromium) with its own profile (~/.config/rose-browser) and a DevTools port
on 127.0.0.1 only. Chromium refuses remote control of your main profile, and keeping ROSE's sessions
separate is safer anyway: log in once in ROSE's browser to the sites you want ROSE to use (e.g.
WhatsApp Web). There is no "run arbitrary JavaScript" tool: only fixed actions (open, read, click by
visible text, fill a field, press a key), with user text passed as data, never as code.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.request
from itertools import count
from pathlib import Path
from urllib.parse import urlparse

from rose.common import ToolError, display_env
from rose.config import load_config

PROFILE = Path.home() / ".config" / "rose-browser"
_ids = count(1)

# Fixed page scripts. Inputs are injected with json.dumps, so they're always JS string literals.
FIND = """
(() => {
  const want = %s.toLowerCase().trim();
  const sel = 'button,a,[role=button],[role=link],[role=tab],[role=menuitem],input[type=submit],input[type=button],[aria-label],[title],[data-testid]';
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; };
  const label = e => (e.getAttribute('aria-label') || e.innerText || e.value || e.title || '').trim();
  let els = [...document.querySelectorAll(sel)].filter(vis);
  let hit = els.filter(e => label(e).toLowerCase() === want);
  if (!hit.length) hit = els.filter(e => label(e).toLowerCase().includes(want));
  if (!hit.length) { try { hit = [...document.querySelectorAll(%s)].filter(vis); } catch (e) {} }
  return hit;
})()
"""
CLICK = "(() => { const h = %s; if (!h.length) return 'NOTFOUND'; if (h.length > 1 && h.slice(1).some(e => !h[0].contains(e))) { return 'MANY:' + h.slice(0,6).map(e => (e.getAttribute('aria-label')||e.innerText||'').trim().slice(0,40)).join(' | '); } h[0].scrollIntoView({block:'center'}); h[0].click(); return 'OK:' + (h[0].getAttribute('aria-label')||h[0].innerText||h[0].tagName).trim().slice(0,60); })()"
FOCUS_FIELD = """
(() => {
  const want = %s.toLowerCase().trim();
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const fields = [...document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]),textarea,[contenteditable=true],[contenteditable=""],[role=textbox]')].filter(vis);
  const lab = e => [e.getAttribute('aria-label'), e.placeholder, e.name, e.id, e.getAttribute('title'),
                    e.labels && e.labels[0] && e.labels[0].innerText, e.getAttribute('data-tab')].filter(Boolean).join(' ').toLowerCase();
  let f = want ? fields.filter(e => lab(e).includes(want)) : [];
  if (!f.length && want) { try { f = [...document.querySelectorAll(%s)].filter(vis); } catch (e) {} }
  if (!f.length && !want) f = fields.filter(e => e === document.activeElement).concat(fields.slice(-1));
  if (!f.length) return 'NOTFOUND:' + fields.slice(0, 8).map(lab).join(' | ');
  const e = f[0]; e.scrollIntoView({block:'center'}); e.focus();
  if (e.isContentEditable) { const r = document.createRange(); r.selectNodeContents(e); const s = getSelection(); s.removeAllRanges(); s.addRange(r); document.execCommand('delete'); }
  else { e.select && e.select(); e.value = ''; e.dispatchEvent(new Event('input', {bubbles: true})); }
  return 'OK:' + lab(e).slice(0, 60);
})()
"""
READ = "(() => { const t = document.body ? document.body.innerText : ''; return JSON.stringify({title: document.title, url: location.href, text: t.slice(0, %d)}); })()"


def _port() -> int:
    return int(load_config().get("browser_port", 9222))


def _http(path: str, method: str = "GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{_port()}{path}", method=method)
    with urllib.request.urlopen(req, timeout=5) as r:
        body = r.read()
    try:
        return json.loads(body or b"null")
    except ValueError:  # some endpoints (e.g. /json/close) answer with plain text
        return body.decode(errors="replace")


def _running() -> bool:
    try:
        _http("/json/version")
        return True
    except Exception:
        return False


def _binary() -> str:
    for b in (load_config().get("browser_binary") or "", "brave-browser", "google-chrome", "chromium", "chromium-browser"):
        if b and shutil.which(b):
            return shutil.which(b)
    raise ToolError("No Chromium-based browser found (Brave, Chrome or Chromium).")


def _check_url(url: str) -> str:
    u = urlparse(url if "://" in url else "https://" + url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ToolError("Only http(s) URLs.")
    return u.geturl()


def browser_start(url: str = "") -> str:
    """Start ROSE's own browser (separate profile; log in there once to sites ROSE should use)."""
    if _running():
        if url:
            return browser_open(url)
        return "ROSE's browser is already running."
    PROFILE.mkdir(parents=True, exist_ok=True)
    cmd = [_binary(), f"--remote-debugging-port={_port()}", "--remote-debugging-address=127.0.0.1",
           f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check", "--class=ROSE-Browser"]
    if url:
        cmd.append(_check_url(url))
    subprocess.Popen(cmd, env=display_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(40):
        if _running():
            return "Started ROSE's browser" + (f" on {url}" if url else "") + "."
        time.sleep(0.25)
    raise ToolError("The browser didn't open its DevTools port. Is another instance using the ROSE profile?")


def _tabs() -> list[dict]:
    if not _running():
        raise ToolError("ROSE's browser isn't running. Call browser_start first.")
    return [t for t in _http("/json/list") if t.get("type") == "page"]


def browser_tabs() -> list[dict]:
    """Open tabs in ROSE's browser: id, title, url."""
    return [{"tab": t["id"][:8], "title": t.get("title", "")[:100], "url": t.get("url", "")[:200]} for t in _tabs()]


def _tab(tab: str) -> dict:
    tabs = _tabs()
    if not tabs:
        raise ToolError("No tabs open.")
    if not tab:
        return tabs[0]  # most recently focused first
    t = tab.lower()
    hit = [x for x in tabs if x["id"].lower().startswith(t)] or \
          [x for x in tabs if t in x.get("title", "").lower() or t in x.get("url", "").lower()]
    if not hit:
        raise ToolError(f"No tab matches '{tab}'. Tabs: {[x.get('title', '')[:40] for x in tabs]}")
    return hit[0]


def _cdp(tab: dict, calls: list[tuple[str, dict]]) -> list:
    from websockets.sync.client import connect
    out = []
    with connect(tab["webSocketDebuggerUrl"], open_timeout=5, max_size=16 * 1024 * 1024) as ws:
        for method, params in calls:
            mid = next(_ids)
            ws.send(json.dumps({"id": mid, "method": method, "params": params}))
            deadline = time.time() + 20
            while time.time() < deadline:
                msg = json.loads(ws.recv(timeout=20))
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise ToolError(f"Browser error: {msg['error'].get('message')}")
                    out.append(msg.get("result", {}))
                    break
    return out


def _eval(tab: dict, expr: str):
    r = _cdp(tab, [("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})])[0]
    if r.get("exceptionDetails"):
        raise ToolError("The page script failed: " + str(r["exceptionDetails"].get("text"))[:200])
    return r.get("result", {}).get("value")


def browser_open(url: str, new_tab: bool = True) -> str:
    """Open a URL in ROSE's browser (starts it if needed)."""
    url = _check_url(url)
    if not _running():
        return browser_start(url)
    if new_tab:
        _http(f"/json/new?{url}", method="PUT")
    else:
        _cdp(_tab(""), [("Page.navigate", {"url": url})])
    return f"Opened {url}."


def browser_read(tab: str = "", max_chars: int = 8000) -> dict:
    """Read a tab's visible text (title, url, text) without a screenshot."""
    t = _tab(tab)
    return json.loads(_eval(t, READ % max(500, min(max_chars, 50000))))


def browser_click(text: str, tab: str = "") -> str:
    """Click a button/link in a tab by its visible text or label (e.g. "Send", "Next"), or a CSS selector."""
    t = _tab(tab)
    found = FIND % (json.dumps(text), json.dumps(text))
    res = _eval(t, CLICK % found)
    if res == "NOTFOUND":
        raise ToolError(f"Nothing clickable labelled '{text}'. Use browser_read to see the page.")
    if res.startswith("MANY:"):
        raise ToolError(f"Several matches for '{text}': {res[5:]}. Be more specific.")
    return f"Clicked {res[3:]!r} in {t.get('title', '')[:50]}."


def browser_fill(field: str, text: str, tab: str = "") -> str:
    """Type text into a field found by label/placeholder/name (e.g. "Search", "Type a message"), or the
    focused field if `field` is empty. Replaces its contents; doesn't submit."""
    if len(text) > 10000:
        raise ToolError("Text too long.")
    t = _tab(tab)
    res = _eval(t, FOCUS_FIELD % (json.dumps(field), json.dumps(field)))
    if res.startswith("NOTFOUND"):
        raise ToolError(f"No field matches '{field}'. Fields on the page: {res[9:][:200]}")
    _cdp(t, [("Input.insertText", {"text": text})])  # real typing events: works in React/WhatsApp editors
    return f"Filled {res[3:]!r} with {len(text)} characters."


def browser_press(key: str, tab: str = "", confirm: bool = False) -> str:
    """Press a key in a tab: Enter, Tab, Escape, ArrowDown, ArrowUp. Enter may send/submit: needs confirm=true."""
    keys = {"enter": ("Enter", 13, "\r"), "tab": ("Tab", 9, ""), "escape": ("Escape", 27, ""),
            "arrowdown": ("ArrowDown", 40, ""), "arrowup": ("ArrowUp", 38, "")}
    k = keys.get(key.lower())
    if not k:
        raise ToolError(f"Key must be one of {list(keys)}.")
    if k[0] == "Enter" and not confirm:
        raise ToolError("Enter may send a message or submit a form. Ask the user, then call again with confirm=true.")
    t = _tab(tab)
    base = {"key": k[0], "code": k[0], "windowsVirtualKeyCode": k[1], "nativeVirtualKeyCode": k[1]}
    _cdp(t, [("Input.dispatchKeyEvent", {"type": "keyDown", **base, **({"text": k[2]} if k[2] else {})}),
             ("Input.dispatchKeyEvent", {"type": "keyUp", **base})])
    return f"Pressed {k[0]}."


def browser_close_tab(tab: str) -> str:
    """Close a tab in ROSE's browser."""
    t = _tab(tab)
    last = len(_tabs()) == 1
    _http(f"/json/close/{t['id']}")
    return f"Closed {t.get('title', '')[:60]}." + (" That was the last tab, so ROSE's browser closed too." if last else "")


def browser_status() -> dict:
    """Whether ROSE's browser is running, and its tabs."""
    if not _running():
        return {"running": False, "how": "browser_start opens it (separate profile; log in there once)."}
    return {"running": True, "profile": str(PROFILE), "tabs": browser_tabs()}
