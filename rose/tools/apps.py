"""Apps, URLs and windows (X11, via EWMH messages through python-xlib)."""

from __future__ import annotations

import configparser
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from rose.common import ToolError, display_env, require, session_type

# ---------- apps ----------

_APP_DIRS = [
    Path.home() / ".local/share/applications",
    Path.home() / ".local/share/flatpak/exports/share/applications",
    Path("/var/lib/flatpak/exports/share/applications"),
    Path("/var/lib/snapd/desktop/applications"),
    Path("/usr/local/share/applications"),
    Path("/usr/share/applications"),
]


def _installed_apps() -> dict[str, str]:
    """Map desktop-file id -> display name for launchable apps (first match wins, like XDG)."""
    apps: dict[str, str] = {}
    for d in _APP_DIRS:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.desktop")):
            app_id = f.stem
            if app_id in apps:
                continue
            cp = configparser.RawConfigParser(strict=False, interpolation=None)
            try:
                cp.read(f, encoding="utf-8")
                e = cp["Desktop Entry"]
            except (configparser.Error, KeyError, UnicodeDecodeError):
                continue
            if e.get("Type") != "Application" or e.get("NoDisplay") == "true" or e.get("Hidden") == "true":
                continue
            apps[app_id] = e.get("Name", app_id)
    return apps


def list_apps(filter: str = "") -> str:
    """List installed desktop apps as 'app_id: Name'. Use the app_id with open_app."""
    f = filter.lower()
    rows = [f"{i}: {n}" for i, n in sorted(_installed_apps().items(), key=lambda kv: kv[1].lower())
            if f in i.lower() or f in n.lower()]
    return "\n".join(rows) if rows else "No matching apps."


def open_app(app: str) -> str:
    """Launch an installed desktop app by app_id or name (no arguments, no shell)."""
    apps = _installed_apps()
    q = app.lower().removesuffix(".desktop")
    match = next((i for i in apps if i.lower() == q), None) or next(
        (i for i, n in apps.items() if n.lower() == q), None
    )
    if match is None:
        candidates = [i for i, n in apps.items() if q in i.lower() or q in n.lower()]
        if len(candidates) == 1:
            match = candidates[0]
        elif candidates:
            raise ToolError(f"'{app}' matches several apps: " + ", ".join(f"{c} ({apps[c]})" for c in candidates[:10]))
        else:
            raise ToolError(f"No installed app matches '{app}'. Use list_apps to see what's available.")
    require("gtk-launch", "sudo apt install libgtk-3-bin")
    subprocess.Popen(["gtk-launch", match], env=display_env(), stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return f"Launched {apps[match]} ({match})."


def open_url(url: str) -> str:
    """Open an http(s) URL in the default browser."""
    u = urlparse(url if "://" in url else "https://" + url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ToolError("Only http:// and https:// URLs can be opened.")
    subprocess.Popen(["xdg-open", u.geturl()], env=display_env(), stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return f"Opened {u.geturl()} in the default browser."


# ---------- windows (X11 / EWMH) ----------

def _x():
    if session_type() != "x11":
        raise ToolError("Window tools currently support X11 only.")
    from Xlib import X, display, protocol

    os.environ.setdefault("DISPLAY", display_env()["DISPLAY"])
    d = display.Display(display_env()["DISPLAY"])
    return d, X, protocol


def _prop(d, win, name, kind=None):
    atom = d.intern_atom(name)
    p = win.get_full_property(atom, kind if kind is not None else 0)
    return p.value if p is not None else None


def _title(d, win) -> str:
    v = _prop(d, win, "_NET_WM_NAME", d.intern_atom("UTF8_STRING"))
    if v is None:
        v = _prop(d, win, "WM_NAME")
    if isinstance(v, bytes):
        v = v.decode(errors="replace")
    return v or ""


def _send(d, X, protocol, win, msg: str, data: list[int]) -> None:
    root = d.screen().root
    ev = protocol.event.ClientMessage(window=win, client_type=d.intern_atom(msg),
                                      data=(32, (data + [0] * 5)[:5]))
    root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
    d.flush()


def _windows(d):
    root = d.screen().root
    active = _prop(d, root, "_NET_ACTIVE_WINDOW")
    active_id = active[0] if active is not None and len(active) else None
    out = []
    for wid in _prop(d, root, "_NET_CLIENT_LIST") or []:
        w = d.create_resource_object("window", wid)
        try:
            types = _prop(d, w, "_NET_WM_WINDOW_TYPE") or []
            if d.intern_atom("_NET_WM_WINDOW_TYPE_DOCK") in types or d.intern_atom("_NET_WM_WINDOW_TYPE_DESKTOP") in types:
                continue
            cls = w.get_wm_class() or ("", "")
            pid = _prop(d, w, "_NET_WM_PID")
            g = w.get_geometry()
            pos = g.root.translate_coords(w, 0, 0)
            out.append({
                "id": hex(wid),
                "title": _title(d, w),
                "app": cls[1] if len(cls) > 1 else "",
                "pid": int(pid[0]) if pid is not None and len(pid) else None,
                "x": pos.x, "y": pos.y, "width": g.width, "height": g.height,
                "active": wid == active_id,
            })
        except Exception:
            continue
    return out


def list_windows() -> list[dict]:
    """List open windows with id, title, app class, pid, position/size and which one is active."""
    d, _, _ = _x()
    try:
        return _windows(d)
    finally:
        d.close()


def _find(d, window: str) -> dict:
    wins = _windows(d)
    w = window.strip().lower()
    if w.startswith("0x") or w.isdigit():
        wid = int(w, 16) if w.startswith("0x") else int(w)
        for x in wins:
            if int(x["id"], 16) == wid:
                return x
        raise ToolError(f"No window with id {window}. Use list_windows.")
    hits = [x for x in wins if w in x["title"].lower() or w == x["app"].lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ToolError(f"No window matches '{window}'. Use list_windows.")
    raise ToolError(f"'{window}' matches {len(hits)} windows; pass an id instead: "
                    + "; ".join(f"{h['id']} {h['title'][:60]}" for h in hits[:10]))


def focus_window(window: str) -> str:
    """Bring a window to the front and focus it. `window` is an id from list_windows or part of its title."""
    d, X, protocol = _x()
    try:
        target = _find(d, window)
        wobj = d.create_resource_object("window", int(target["id"], 16))
        # source indication 2 = pager/user action, so window managers don't refuse the request.
        _send(d, X, protocol, wobj, "_NET_ACTIVE_WINDOW", [2, X.CurrentTime, 0])
        time.sleep(0.3)
        active = _prop(d, d.screen().root, "_NET_ACTIVE_WINDOW")
        ok = active is not None and len(active) and active[0] == int(target["id"], 16)
        return f"{'Focused' if ok else 'Asked the window manager to focus'}: {target['title']} ({target['id']})"
    finally:
        d.close()


def close_window(window: str, confirm: bool = False) -> str:
    """Politely ask a window to close (the app may prompt to save). Needs confirm=true."""
    d, X, protocol = _x()
    try:
        target = _find(d, window)
        if not confirm:
            raise ToolError(f"This would close '{target['title']}' ({target['id']}). Call again with confirm=true.")
        wobj = d.create_resource_object("window", int(target["id"], 16))
        _send(d, X, protocol, wobj, "_NET_CLOSE_WINDOW", [X.CurrentTime, 2])
        return f"Asked '{target['title']}' to close."
    finally:
        d.close()
