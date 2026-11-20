#!/usr/bin/python3
"""AT-SPI helper, run with the *system* Python (which has python3-gi + Atspi). ROSE's own venv calls
it as a subprocess: reads a JSON request on stdin, prints a JSON reply.

Requests:
  {"op": "apps"}
  {"op": "list", "window": "...", "max": 150}           -> actionable elements with ids
  {"op": "read", "window": "...", "max_chars": 6000}    -> visible text
  {"op": "click", "window": "...", "element": "Send"}   -> do the element's default action
  {"op": "set_text", "window": "...", "element": "...", "text": "..."}
"""

import json
import sys

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

ACTIONABLE = {"push button", "toggle button", "check box", "radio button", "menu item", "check menu item",
              "radio menu item", "link", "entry", "text", "password text", "combo box", "spin button",
              "page tab", "list item", "tree item", "table cell", "slider", "button", "toggle switch"}
TEXTUAL = {"entry", "text", "password text", "spin button", "combo box"}


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
                                             ("focused", Atspi.StateType.FOCUSED), ("checked", Atspi.StateType.CHECKED))}
    except Exception:
        return {}


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


def resolve(root, element):
    """element = an id from `list` (e.g. #0.3.1) or (part of) the element's name."""
    if element.startswith("#"):
        path = element[1:]
        if not path or not all(p.isdigit() for p in path.split(".")):
            raise LookupError(f"Bad element id '{element}'.")
        return path, by_path(root, path)
    nodes = []
    walk(root, "", nodes, 5000)
    e = element.lower().strip()
    exact = [(p, n) for p, n in nodes if _name(n).lower() == e and _role(n) in ACTIONABLE]
    part = [(p, n) for p, n in nodes if e in _name(n).lower() and _role(n) in ACTIONABLE]
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
            items.append(item)
            if len(items) >= req.get("max", 150):
                break
    return {"app": app, "window": title, "elements": items}


def do_read(req):
    w, app, title = find_window(req["window"])
    nodes = []
    walk(w, "", nodes, 4000)
    parts, total = [], 0
    for _, n in nodes:
        if not _states(n).get("showing", True):
            continue
        t = ""
        try:
            ti = n.get_text_iface() if hasattr(n, "get_text_iface") else None
            if ti is not None:
                t = Atspi.Text.get_text(n, 0, min(Atspi.Text.get_character_count(n), 2000))
        except Exception:
            t = ""
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


def do_set_text(req):
    w, app, title = find_window(req["window"])
    path, n = resolve(w, req["element"])
    try:
        Atspi.Component.grab_focus(n)
    except Exception:
        pass
    try:
        ok = Atspi.EditableText.set_text_contents(n, req["text"])
        if ok:
            return {"done": f"set text of {_role(n)} '{_name(n)}' ({path})"}
    except Exception:
        pass
    return {"focused_for_typing": f"{_role(n)} '{_name(n)}' ({path})"}  # caller types via xdotool


def main():
    req = json.loads(sys.stdin.read())
    try:
        fn = {"list": do_list, "read": do_read, "click": do_click, "set_text": do_set_text}[req["op"]]
        print(json.dumps({"ok": True, **fn(req)}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{e}"}))


if __name__ == "__main__":
    main()
