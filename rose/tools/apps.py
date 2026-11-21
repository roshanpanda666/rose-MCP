"""Apps, URLs and windows (X11, via EWMH messages through python-xlib)."""

from __future__ import annotations

import configparser
import os
import subprocess
import threading
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
_apps_lock = threading.Lock()
_apps_cache: dict = {"key": None, "apps": {}}


def _dirs_key() -> tuple:
    """Changes whenever a .desktop file is added, removed or replaced in any app folder."""
    key = []
    for d in _APP_DIRS:
        try:
            key.append((str(d), d.stat().st_mtime_ns))
        except OSError:
            key.append((str(d), None))
    return tuple(key)


def _parse_apps() -> dict[str, str]:
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


def _installed_apps() -> dict[str, str]:
    """Map desktop-file id -> display name for launchable apps (first match wins, like XDG).

    Parsing every .desktop file takes ~250 ms, so the result is cached until an app folder changes."""
    key = _dirs_key()
    with _apps_lock:
        if _apps_cache["key"] != key:
            _apps_cache.update(key=key, apps=_parse_apps())
        return dict(_apps_cache["apps"])


def list_apps(filter: str = "") -> str:
    """Installed apps as 'app_id: Name' (use app_id with open_app)."""
    f = filter.lower()
    rows = [f"{i}: {n}" for i, n in sorted(_installed_apps().items(), key=lambda kv: kv[1].lower())
            if f in i.lower() or f in n.lower()]
    return "\n".join(rows) if rows else "No matching apps."


def open_app(app: str) -> str:
    """Launch an installed app by app_id or name."""
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


def _prop(d, win, name: str, kind=None):
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


def _active_id(d) -> int | None:
    active = _prop(d, d.screen().root, "_NET_ACTIVE_WINDOW")
    return active[0] if active is not None and len(active) else None


def _windows(d) -> list[dict]:
    root = d.screen().root
    active_id = _active_id(d)
    skip = {d.intern_atom("_NET_WM_WINDOW_TYPE_DOCK"), d.intern_atom("_NET_WM_WINDOW_TYPE_DESKTOP")}
    out = []
    for wid in _prop(d, root, "_NET_CLIENT_LIST") or []:
        w = d.create_resource_object("window", wid)
        try:
            if skip & set(_prop(d, w, "_NET_WM_WINDOW_TYPE") or []):
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
        except Exception:  # windows can vanish mid-query
            continue
    return out


def list_windows() -> list[dict]:
    """Open windows: id, title, app, pid, geometry, active."""
    d, _, _ = _x()
    try:
        return _windows(d)
    finally:
        d.close()


def active_window() -> dict | None:
    """The active window (as in list_windows), or None if it can't be determined."""
    try:
        return next((w for w in list_windows() if w.get("active")), None)
    except Exception:
        return None


def window_matches(win: dict, window: str) -> bool:
    """Does `win` match `window`: an id (0x... or decimal), part of its title, or its exact app class?"""
    w = window.strip().lower()
    if w.startswith("0x") or w.isdigit():
        try:
            return int(win["id"], 16) == (int(w, 16) if w.startswith("0x") else int(w))
        except ValueError:
            return False
    return w in (win.get("title") or "").lower() or w == (win.get("app") or "").lower()


def _find(d, window: str) -> dict:
    wins = _windows(d)
    w = window.strip().lower()
    if w.startswith("0x") or w.isdigit():
        hit = next((x for x in wins if window_matches(x, w)), None)
        if hit:
            return hit
        raise ToolError(f"No window with id {window}. Use list_windows.")
    hits = [x for x in wins if window_matches(x, w)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ToolError(f"No window matches '{window}'. Use list_windows.")
    raise ToolError(f"'{window}' matches {len(hits)} windows; pass an id instead: "
                    + "; ".join(f"{h['id']} {h['title'][:60]}" for h in hits[:10]))


def focus_window(window: str) -> str:
    """Focus a window (id from list_windows or part of its title)."""
    d, X, protocol = _x()
    try:
        target = _find(d, window)
        wid = int(target["id"], 16)
        wobj = d.create_resource_object("window", wid)
        # source indication 2 = pager/user action, so window managers don't refuse the request.
        _send(d, X, protocol, wobj, "_NET_ACTIVE_WINDOW", [2, X.CurrentTime, 0])
        ok = False
        deadline = time.monotonic() + 0.3  # same 0.3 s budget as before, but return as soon as it's active
        while not ok and time.monotonic() < deadline:
            time.sleep(0.03)
            ok = _active_id(d) == wid
        return f"{'Focused' if ok else 'Asked the window manager to focus'}: {target['title']} ({target['id']})"
    finally:
        d.close()


def close_window(window: str, confirm: bool = False) -> str:
    """Ask a window to close (app may prompt to save). Needs confirm=true."""
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


# ---------- moving windows between monitors ----------

_MOVE_FLAGS = (1 << 8) | (1 << 9) | (1 << 10) | (1 << 11) | (2 << 12)  # x, y, w, h set; source = pager/user
_NORTH_WEST = 1


def target_rect(win: dict, mons: list[dict], monitor: int | None, x: int | None, y: int | None,
                width: int | None, height: int | None) -> tuple[int, int, int, int]:
    """Where a window should go: on `monitor` (same relative offset, size clamped to fit) or at x/y.
    Width/height default to the window's current size."""
    w = max(100, width or win["width"])
    h = max(100, height or win["height"])
    if monitor is None:
        if x is None and y is None:
            raise ToolError("Give monitor=<index> (see list_monitors) or x/y in screen pixels.")
        return (win["x"] if x is None else x), (win["y"] if y is None else y), w, h
    by_index = {m["index"]: m for m in mons}
    if monitor not in by_index:
        raise ToolError(f"No monitor {monitor}. Monitors: {[(m['index'], m['name']) for m in mons] or 'unknown'}")
    dst = by_index[monitor]
    cx, cy = win["x"] + win["width"] // 2, win["y"] + win["height"] // 2
    src = next((m for m in mons if m["x"] <= cx < m["x"] + m["width"] and m["y"] <= cy < m["y"] + m["height"]), None)
    w, h = min(w, dst["width"]), min(h, dst["height"])
    if src is not None and x is None and y is None:  # keep the same place relative to the monitor
        nx, ny = dst["x"] + (win["x"] - src["x"]), dst["y"] + (win["y"] - src["y"])
    else:
        nx = dst["x"] + (x if x is not None else (dst["width"] - w) // 2)
        ny = dst["y"] + (y if y is not None else (dst["height"] - h) // 2)
    nx = max(dst["x"], min(nx, dst["x"] + dst["width"] - w))
    ny = max(dst["y"], min(ny, dst["y"] + dst["height"] - h))
    return nx, ny, w, h


def _set_maximized(d, X, protocol, wobj, on: bool) -> None:
    vert, horz = d.intern_atom("_NET_WM_STATE_MAXIMIZED_VERT"), d.intern_atom("_NET_WM_STATE_MAXIMIZED_HORZ")
    _send(d, X, protocol, wobj, "_NET_WM_STATE", [1 if on else 0, vert, horz, 2])


def move_window(window: str, monitor: int | None = None, x: int | None = None, y: int | None = None,
                width: int | None = None, height: int | None = None, maximize: bool | None = None) -> str:
    """Move/resize a window (id or title part): monitor=<index> (list_monitors) or x/y screen px; maximize=true/false."""
    from rose.tools.screenshot import monitors

    d, X, protocol = _x()
    try:
        target = _find(d, window)
        wid = int(target["id"], 16)
        wobj = d.create_resource_object("window", wid)
        state = set(_prop(d, wobj, "_NET_WM_STATE") or [])
        was_max = d.intern_atom("_NET_WM_STATE_MAXIMIZED_VERT") in state
        nx, ny, w, h = target_rect(target, monitors(), monitor, x, y, width, height)
        if was_max:  # a maximized window ignores move requests
            _set_maximized(d, X, protocol, wobj, False)
            time.sleep(0.1)
        _send(d, X, protocol, wobj, "_NET_MOVERESIZE_WINDOW", [_MOVE_FLAGS | _NORTH_WEST, nx, ny, w, h])
        if maximize or (maximize is None and was_max):
            time.sleep(0.15)
            _set_maximized(d, X, protocol, wobj, True)
        time.sleep(0.3)
        now = next((wi for wi in _windows(d) if int(wi["id"], 16) == wid), None)
    finally:
        d.close()
    where = f"monitor {monitor}" if monitor is not None else f"({nx}, {ny})"
    if now is None:
        return f"Asked to move '{target['title'][:60]}' to {where}."
    return (f"Moved '{target['title'][:60]}' to {where}: now at ({now['x']}, {now['y']}) "
            f"{now['width']}x{now['height']}.")
