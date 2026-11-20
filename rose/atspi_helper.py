#!/usr/bin/python3
"""AT-SPI helper, run with the *system* Python (which has python3-gi + Atspi). ROSE's own venv calls
it as a subprocess: reads a JSON request on stdin, prints a JSON reply.

Requests:
  {"op": "apps"}
  {"op": "list", "window": "...", "max": 150}           -> actionable elements with ids
  {"op": "read", "window": "...", "max_chars": 6000}    -> visible text
  {"op": "read", "window": "...", "element": "#0.3"}    -> one element: role, name, value, states
  {"op": "click", "window": "...", "element": "Send"}   -> do the element's default action
  {"op": "set_text", "window": "...", "element": "...", "text": "..."}  -> set + read the value back
  {"op": "focus", "window": "...", "element": "..."}    -> grab focus, report whether it took
  {"op": "focused", "window": "", "expect": "..."}      -> focused element of the active window;
                                                           with `expect`, whether it contains that text
"""

import json
import sys
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

ACTIONABLE = {"push button", "toggle button", "check box", "radio button", "menu item", "check menu item",
              "radio menu item", "link", "entry", "text", "password text", "combo box", "spin button",
              "page tab", "list item", "tree item", "table cell", "slider", "button", "toggle switch"}
TEXTUAL = {"entry", "text", "password text", "spin button", "combo box", "terminal", "document text",
           "paragraph", "editbar"}
SECRET = {"password text"}  # never return what's in these


def _role(a):
    try:
        return a.get_role_name()
    except Exception:
        return "?"


def _name(a):
    try:
        return (a.get_name() or "").strip()
    except Exception:
        return ""


def _states(a):
    try:
        s = a.get_state_set()
        return {k: s.contains(v) for k, v in (("enabled", Atspi.StateType.ENABLED), ("showing", Atspi.StateType.SHOWING),
                                             ("focused", Atspi.StateType.FOCUSED), ("checked", Atspi.StateType.CHECKED),
                                             ("active", Atspi.StateType.ACTIVE), ("editable", Atspi.StateType.EDITABLE))}
    except Exception:
        return {}


def _text(a, limit=20000, around_caret=False):
    """Text contents of an element with a Text interface ('' if none). Long texts are cut to `limit`
    chars: around the caret when asked (where typing just happened), else the end (terminals)."""
    try:
        if a.get_text_iface() is None:
            return ""
        n = Atspi.Text.get_character_count(a)
        if n <= limit:
            return (Atspi.Text.get_text(a, 0, n) or "").replace("\ufffc", "")  # U+FFFC = embedded child
        start = max(0, n - limit)
        if around_caret:
            try:
                c = Atspi.Text.get_caret_offset(a)
                if c >= 0:
                    start = max(0, min(c - limit // 2, n - limit))
            except Exception:
                pass
        return (Atspi.Text.get_text(a, start, start + limit) or "").replace("\ufffc", "")
    except Exception:
        return ""


def _describe(path, n, value_chars=2000):
    """role/name/value/states of one element, for read-back checks. Never returns secret values."""
    r = _role(n)
    st = _states(n)
    d = {"id": "#" + path if path is not None else None, "role": r, "name": _name(n)[:120],
         "focused": bool(st.get("focused")), "enabled": st.get("enabled", True)}
    if st.get("checked"):
        d["checked"] = True
    if r in SECRET:
        d["value"] = "(hidden: password field)"
    else:
        v = _text(n, around_caret=True)
        if v:
            d["value"] = v if len(v) <= value_chars else v[:value_chars] + f"... ({len(v)} chars)"
    return d


def find_window(query):
    q = (query or "").lower()
    desk = Atspi.get_desktop(0)
    best = None
    for i in range(desk.get_child_count()):
        app = desk.get_child_at_index(i)
        if app is None:
            continue
        appname = _name(app).lower()
        for j in range(app.get_child_count()):
            w = app.get_child_at_index(j)
            if w is None:
                continue
            title = _name(w).lower()
            if q and (q in title or q == appname or q in appname):
                st = _states(w)
                if best is None or st.get("showing"):
                    best = (w, _name(app), _name(w))
    if best is None:
        raise LookupError(f"No accessible window matches '{query}'. Apps: " +
                          ", ".join(sorted({_name(desk.get_child_at_index(i)) for i in range(desk.get_child_count())
                                            if desk.get_child_at_index(i)} - {""})))
    return best


def walk(node, path, out, limit, depth=0):
    if len(out) >= limit or depth > 40:
        return
    try:
        n = node.get_child_count()
    except Exception:
        return
    for i in range(min(n, 400)):
        try:
            c = node.get_child_at_index(i)
        except Exception:
            continue
        if c is None:
            continue
        p = f"{path}.{i}" if path else str(i)
        out.append((p, c))
        walk(c, p, out, limit, depth + 1)


def by_path(root, path):
    node = root
    for part in path.split("."):
        node = node.get_child_at_index(int(part))
        if node is None:
            raise LookupError(f"Element {path} is gone (window changed?). List again.")
    return node


def resolve(root, element, any_role=False):
    """element = an id from `list` (e.g. #0.3.1) or (part of) the element's name."""
    if element.startswith("#"):
        path = element[1:]
        if not path or not all(p.isdigit() for p in path.split(".")):
            raise LookupError(f"Bad element id '{element}'.")
        return path, by_path(root, path)
    nodes = []
    walk(root, "", nodes, 5000)
    e = element.lower().strip()
    ok_role = (lambda n: True) if any_role else (lambda n: _role(n) in ACTIONABLE or _role(n) in TEXTUAL)
    exact = [(p, n) for p, n in nodes if _name(n).lower() == e and ok_role(n)]
    part = [(p, n) for p, n in nodes if e in _name(n).lower() and ok_role(n)]
    hits = exact or part
    hits = [h for h in hits if _states(h[1]).get("showing", True)] or hits
    if not hits:
        raise LookupError(f"No element named '{element}'. Use ui_list to see what's there.")
    if len(hits) > 1 and not exact:
        raise LookupError(f"'{element}' matches several: " + "; ".join(f"{p} {_role(n)} '{_name(n)}'" for p, n in hits[:8]))
    return hits[0]


def do_list(req):
    w, app, title = find_window(req["window"])
    nodes = []
    walk(w, "", nodes, 4000)
    items = []
    for p, n in nodes:
        r = _role(n)
        nm = _name(n)
        if r in ACTIONABLE and (nm or r in TEXTUAL):
            st = _states(n)
            if not st.get("showing", True):
                continue
            item = {"id": "#" + p, "role": r, "name": nm[:80]}
            if not st.get("enabled", True):
                item["disabled"] = True
            if st.get("checked"):
                item["checked"] = True
            if st.get("focused"):
                item["focused"] = True
            if r in TEXTUAL and r not in SECRET:
                v = _text(n, limit=200, around_caret=True).strip()
                if v:
                    item["value"] = v[:80]
            items.append(item)
            if len(items) >= req.get("max", 150):
                break
    return {"app": app, "window": title, "elements": items}


def do_read(req):
    w, app, title = find_window(req["window"])
    if req.get("element"):
        path, n = resolve(w, req["element"], any_role=True)
        return {"app": app, "window": title, "window_active": bool(_states(w).get("active")),
                "element": _describe(path, n, value_chars=req.get("max_chars", 6000))}
    nodes = []
    walk(w, "", nodes, 4000)
    parts, total = [], 0
    for _, n in nodes:
        if not _states(n).get("showing", True):
            continue
        t = "" if _role(n) in SECRET else _text(n, limit=2000)
        t = (t or _name(n)).strip()
        if t and (not parts or parts[-1] != t):
            parts.append(t)
            total += len(t)
            if total > req.get("max_chars", 6000):
                break
    return {"app": app, "window": title, "text": "\n".join(parts)}


def do_click(req):
    w, app, title = find_window(req["window"])
    path, n = resolve(w, req["element"])
    try:
        act = n.get_action_iface() if hasattr(n, "get_action_iface") else None
        count = Atspi.Action.get_n_actions(n) if act is not None else 0
    except Exception:
        count = 0
    if count:
        names = [Atspi.Action.get_action_name(n, i) for i in range(count)]
        idx = next((i for i, a in enumerate(names) if a in ("click", "press", "activate", "jump", "toggle")), 0)
        Atspi.Action.do_action(n, idx)
        return {"done": f"{names[idx]} on {_role(n)} '{_name(n)}' ({path}) in {title}"}
    ext = n.get_extents(Atspi.CoordType.SCREEN)
    if ext.width <= 0:
        raise LookupError("Element has no action and no position; can't click it.")
    return {"click_at": [ext.x + ext.width // 2, ext.y + ext.height // 2], "element": f"{_role(n)} '{_name(n)}' ({path})"}


def _grab_focus(n):
    try:
        Atspi.Component.grab_focus(n)
    except Exception:
        pass
    time.sleep(0.1)
    return bool(_states(n).get("focused"))


def do_set_text(req):
    w, app, title = find_window(req["window"])
    path, n = resolve(w, req["element"])
    focused = _grab_focus(n)
    base = {"id": "#" + path, "element": f"{_role(n)} '{_name(n)}' ({path})", "window": title, "app": app}
    try:
        if n.get_editable_text_iface() is not None and Atspi.EditableText.set_text_contents(n, req["text"]):
            time.sleep(0.1)
            back = _describe(path, n, value_chars=len(req["text"]) + 200)
            return {**base, "done": f"set text of {base['element']}", "readback": back}
    except Exception:
        pass
    # No editable-text interface (common in Electron): the caller types via xdotool, after checking
    # that this element really has keyboard focus.
    return {**base, "focused_for_typing": base["element"], "focused": focused}


def do_focus(req):
    w, app, title = find_window(req["window"])
    path, n = resolve(w, req["element"])
    return {"id": "#" + path, "window": title, "app": app, "window_active": bool(_states(w).get("active")),
            "focused": _grab_focus(n)}


def _active_windows(query=""):
    q = (query or "").lower()
    desk = Atspi.get_desktop(0)
    for i in range(desk.get_child_count()):
        app = desk.get_child_at_index(i)
        if app is None:
            continue
        for j in range(app.get_child_count()):
            w = app.get_child_at_index(j)
            if w is None:
                continue
            if q:
                if q in _name(w).lower() or q in _name(app).lower():
                    yield app, w
            elif _states(w).get("active"):
                yield app, w


def do_focused(req):
    """The element that has keyboard focus in the active window (or in `window`), for read-back."""
    for app, w in _active_windows(req.get("window", "")):
        nodes = []
        walk(w, "", nodes, 6000)
        for p, n in nodes:
            st = _states(n)
            if st.get("focused") and st.get("showing", True):
                d = _describe(p, n, value_chars=0)  # value not returned by default
                d.pop("value", None)
                out = {"app": _name(app), "window": _name(w), "window_active": bool(_states(w).get("active")),
                       "element": d}
                exp = req.get("expect")
                if exp is not None:
                    if _role(n) in SECRET:
                        out["contains"] = None
                    else:
                        hay = " ".join(_text(n, limit=max(20000, len(exp) * 4), around_caret=True).split())
                        out["contains"] = " ".join(exp.split()) in hay
                        out["has_text"] = n.get_text_iface() is not None
                return out
        return {"app": _name(app), "window": _name(w), "window_active": bool(_states(w).get("active")),
                "element": None}
    return {"app": None, "window": None, "element": None}


def main():
    req = json.loads(sys.stdin.read())
    try:
        fn = {"list": do_list, "read": do_read, "click": do_click, "set_text": do_set_text,
              "focus": do_focus, "focused": do_focused}[req["op"]]
        print(json.dumps({"ok": True, **fn(req)}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{e}"}))


if __name__ == "__main__":
    main()
