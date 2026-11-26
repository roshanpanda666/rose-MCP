"""Phone actions by name, not by guessed pixels: call someone, message them on WhatsApp/SMS, tap a button by its
label. Built on Android itself:

* contacts: `content query` on the contacts provider (read-only);
* call: the CALL intent with tel:<number>;
* WhatsApp: api.whatsapp.com/send?phone=..&text=.. opened in WhatsApp (chat opens with the message typed in);
  SMS: the SENDTO smsto: intent with sms_body;
* buttons: `uiautomator dump` gives every on-screen element with its text / content-desc / bounds (tree-first,
  like ui_* on the laptop); we tap the centre of the matched element.

Every message is read back before Send (the input box must hold exactly the text) and after (box empty, text in
the chat). Calling and sending need confirm=true: Rose passes it only when the user clearly asked.
"""

from __future__ import annotations

import difflib
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote

from rose.common import ToolError
from rose.config import load_config
from rose.tools import phone

MAX_MSG = 2000
SENDISH = re.compile(r"^(send|call|pay|delete|remove|post|submit|confirm|buy|place order|transfer)\b", re.I)
_BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


# ---------------- screen tree ----------------

def _ui() -> list[dict]:
    """Every element on the phone screen (uiautomator dump)."""
    raw = phone._adb("exec-out", "uiautomator", "dump", "/dev/tty", timeout=20)
    xml = raw[raw.find("<?xml"):raw.rfind(">") + 1] if "<?xml" in raw else ""
    if not xml:
        raise ToolError("Couldn't read the phone screen (is it awake and unlocked?).")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise ToolError("The phone sent an unreadable screen tree; try again.") from None
    out = []
    for n in root.iter("node"):
        m = _BOUNDS.match(n.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        out.append({"text": n.get("text", ""), "desc": n.get("content-desc", ""), "id": n.get("resource-id", ""),
                    "cls": n.get("class", "").rsplit(".", 1)[-1], "clickable": n.get("clickable") == "true",
                    "pkg": n.get("package", ""), "x": (x1 + x2) // 2, "y": (y1 + y2) // 2})
    return out


def _wait_ui(pred, seconds: float):
    end = time.monotonic() + seconds
    while True:
        try:
            nodes = _ui()
            hit = pred(nodes)
            if hit:
                return hit
        except ToolError:
            pass
        if time.monotonic() > end:
            return None
        time.sleep(0.6)


def _tap(node: dict) -> None:
    phone._shell("input", "tap", str(node["x"]), str(node["y"]))


def _awake() -> None:
    phone._shell("input", "keyevent", "KEYCODE_WAKEUP")
    out = phone._adb("shell", "dumpsys window | grep -iE 'mDreamingLockscreen|isKeyguardShowing|mShowingLockscreen'")
    if re.search(r"(isKeyguardShowing|mDreamingLockscreen|mShowingLockscreen)=true", out):
        raise ToolError("The phone is locked. Unlock it first, then ask again.")


def phone_ui_list(filter: str = "") -> list[dict]:
    """Buttons/labels on the phone screen (text, description, clickable), from the screen tree; filter by text."""
    _awake()
    f = filter.strip().lower()
    items = []
    for n in _ui():
        label = n["text"] or n["desc"]
        if not label or (f and f not in label.lower()):
            continue
        items.append({"text": n["text"][:80], "desc": n["desc"][:80], "id": n["id"].rsplit("/", 1)[-1],
                      "clickable": n["clickable"]})
    return items[:120]


def phone_click_text(target: str, pick: int = 0, confirm: bool = False) -> str:
    """Tap a phone button/item by its visible text or description (exact screen-tree position, no guessing).
    Several matches: lists them; pass pick=N. Send/Call/Pay-like buttons need confirm."""
    t = target.strip().lower()
    if not t:
        raise ToolError("Say what to tap.")
    _awake()
    nodes = [n for n in _ui() if n["text"] or n["desc"]]
    exact = [n for n in nodes if t in (n["text"].lower(), n["desc"].lower())]
    hits = exact or [n for n in nodes if t in n["text"].lower() or t in n["desc"].lower()]
    if not hits:
        raise ToolError(f"Nothing labelled '{target}' on the phone screen. phone_ui_list shows what's there.")
    if len(hits) > 1 and not pick:
        lines = "\n".join(f"{i}. {(n['text'] or n['desc'])[:60]} ({n['cls']}, y={n['y']})" for i, n in enumerate(hits, 1))
        raise ToolError(f"Several matches for '{target}':\n{lines}\nCall again with pick=N.")
    if pick and not 1 <= pick <= len(hits):
        raise ToolError(f"pick must be 1..{len(hits)}.")
    n = hits[(pick or 1) - 1]
    label = n["text"] or n["desc"]
    if SENDISH.search(label) and not confirm:
        raise ToolError(f"'{label}' may send, call or pay. If the user asked for it, call again with confirm=true.")
    _tap(n)
    return f"Tapped '{label[:60]}' on the phone."


# ---------------- contacts ----------------

def _cc() -> str:
    return re.sub(r"\D", "", str(load_config().get("phone_country_code", "91"))) or "91"


def _norm_number(raw: str) -> str:
    """'+91 98765-43210', '098765 43210', '9876543210' -> '+919876543210'."""
    s = raw.strip()
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    if s.startswith("+"):
        return "+" + digits
    if s.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        return "+" + _cc() + digits[1:]
    if len(digits) == 10:
        return "+" + _cc() + digits
    return "+" + digits if len(digits) > 10 else digits


_contacts_cache: tuple[float, dict[str, dict]] | None = None
CONTACTS_TTL = 300.0  # `content query` boots a Java VM on the phone (~2 s); contacts rarely change mid-task


def _contacts() -> dict[str, dict]:
    """Phone contacts by name (kept in memory for CONTACTS_TTL; never written to disk)."""
    global _contacts_cache
    if _contacts_cache and time.monotonic() - _contacts_cache[0] < CONTACTS_TTL:
        return _contacts_cache[1]
    people = _contacts_uncached()
    if people:
        _contacts_cache = (time.monotonic(), people)
    return people


def _contacts_uncached() -> dict[str, dict]:
    out = phone._adb("shell", "content query --uri content://com.android.contacts/data/phones "
                              "--projection display_name:data1:is_primary:is_super_primary", timeout=20)
    people: dict[str, dict] = {}
    for line in out.splitlines():
        m = re.search(r"display_name=(.*?), data1=(.*?), is_primary=(\d+), is_super_primary=(\d+)", line)
        if not m:
            continue
        name, num = m.group(1).strip(), _norm_number(m.group(2))
        if not name or not num or name == "NULL":
            continue
        p = people.setdefault(name, {"name": name, "numbers": [], "primary": ""})
        if num not in p["numbers"]:
            p["numbers"].append(num)
        if m.group(3) == "1" or m.group(4) == "1":
            p["primary"] = num
    return people


def _find(who: str, people: dict[str, dict]) -> list[dict]:
    q = re.sub(r"\s+", " ", who.strip().lower())
    names = list(people)
    exact = [n for n in names if n.lower() == q]
    if exact:
        return [people[n] for n in exact]
    words = q.split()
    contains = [n for n in names if all(w in n.lower() for w in words)]
    if contains:
        starts = [n for n in contains if n.lower().startswith(q)]
        return [people[n] for n in (starts if len(starts) == 1 else contains)]
    close = difflib.get_close_matches(q, [n.lower() for n in names], n=5, cutoff=0.72)
    return [people[n] for n in names if n.lower() in close]


def phone_contacts(query: str) -> list[dict]:
    """Search the phone's contacts by name (fuzzy): names and numbers."""
    if not query.strip():
        raise ToolError("Give a name to search for.")
    return [{"name": p["name"], "numbers": p["numbers"][:4]} for p in _find(query, _contacts())[:10]]


def _resolve(who: str) -> tuple[str, str]:
    """(display name, +E.164 number) for a contact name or a number; ToolError if unclear."""
    if re.fullmatch(r"[\d\s()+-]{7,}", who.strip()):
        return who.strip(), _norm_number(who)
    hits = _find(who, _contacts())
    if not hits:
        raise ToolError(f"No contact like '{who}' on the phone. phone_contacts searches them.")
    if len(hits) > 1:
        raise ToolError("Several contacts match: " + "; ".join(f"{p['name']} ({', '.join(p['numbers'][:2])})"
                                                                for p in hits[:6]) + ". Ask the user which one.")
    p = hits[0]
    if p["primary"]:
        return p["name"], p["primary"]
    if len(p["numbers"]) > 1:
        raise ToolError(f"{p['name']} has several numbers: {', '.join(p['numbers'][:4])}. Ask the user which one "
                        "and pass it as who.")
    return p["name"], p["numbers"][0]


# ---------------- call / message ----------------

def phone_call(who: str, confirm: bool = False) -> str:
    """Call a contact (name, fuzzy) or number from the phone. Needs confirm=true (pass it when the user asked)."""
    name, number = _resolve(who)
    if not confirm:
        raise ToolError(f"This calls {name} ({number}). If the user asked for it, call again with confirm=true.")
    _awake()
    phone._shell("am", "start", "-a", "android.intent.action.CALL", "-d", f"tel:{number}")
    time.sleep(2.5)
    state = phone._adb("shell", "dumpsys telephony.registry | grep -m1 mCallState")
    if "mCallState=0" in state:
        return f"Asked the phone to call {name} ({number}), but no call started (check the phone)."
    return f"Calling {name} ({number}) on the phone."


def _find_node(nodes, ids=(), descs=(), texts=()):
    for n in nodes:
        if (n["id"] and n["id"].rsplit("/", 1)[-1] in ids) or (n["desc"] and n["desc"].lower() in descs) \
                or (n["text"] and n["text"].lower() in texts):
            return n
    return None


def phone_message(who: str, message: str, app: str = "whatsapp", confirm: bool = False) -> str:
    """Send a message to a contact (name, fuzzy) or number: app whatsapp (default) or sms. Opens the chat with the
    text, checks it, taps Send, checks it went. Needs confirm=true (pass it when the user asked)."""
    message = message.strip()
    if not message:
        raise ToolError("Give the message text.")
    if len(message) > MAX_MSG:
        raise ToolError(f"Message too long ({len(message)} chars, max {MAX_MSG}).")
    app = app.strip().lower()
    if app not in ("whatsapp", "sms"):
        raise ToolError("app must be 'whatsapp' or 'sms'.")
    name, number = _resolve(who)
    if not confirm:
        raise ToolError(f"This sends to {name} ({number}) on {app}: \"{message[:120]}\". If the user asked for it, "
                        "call again with confirm=true.")
    _awake()
    if app == "whatsapp":
        url = f"https://api.whatsapp.com/send?phone={number.lstrip('+')}&text={quote(message, safe='')}"
        phone._shell("am", "start", "-a", "android.intent.action.VIEW", "-d", url, "-p", "com.whatsapp")
        ids, descs = ("send",), ("send",)
        box_ids = ("entry",)
    else:
        phone._shell("am", "start", "-a", "android.intent.action.SENDTO", "-d", f"smsto:{number}",
                     "--es", "sms_body", message)
        ids, descs = ("compose_message_send_button", "send_message_button_icon"), ("send sms", "send message", "send")
        box_ids = ("compose_message_text",)

    def ready(nodes):
        if any("isn't on whatsapp" in (n["text"] or "").lower() or "not on whatsapp" in (n["text"] or "").lower()
               for n in nodes):
            return "notwa"
        send = _find_node(nodes, ids=ids, descs=descs)
        return (send, nodes) if send else None

    got = _wait_ui(ready, 12)
    if got == "notwa":
        raise ToolError(f"{name} ({number}) isn't on WhatsApp. Try app='sms'.")
    if not got:
        raise ToolError(f"The {app} chat with {name} didn't open with a Send button. Nothing was sent; check the phone.")
    send, nodes = got
    box = next((n for n in nodes if n["id"].rsplit("/", 1)[-1] in box_ids or n["cls"] == "EditText"), None)
    if box is None:  # no read-back possible: never send unchecked
        raise ToolError(f"Couldn't find the {app} message box to check the text. Nothing was sent; check the phone.")
    typed = box.get("text", "")
    if typed.strip() != message.strip():  # read-back before sending
        raise ToolError(f"The message box shows \"{typed[:80]}\", not your text. Nothing was sent; check the phone.")
    _tap(send)

    def sent(nodes):
        b = next((n for n in nodes if n["id"].rsplit("/", 1)[-1] in box_ids or n["cls"] == "EditText"), None)
        empty = b is None or not b["text"].strip() or b["text"].strip() != message.strip()
        return empty and any(message.strip()[:60] in (n["text"] or "") for n in nodes)

    if _wait_ui(sent, 6):
        return f"Sent to {name} ({number}) on {app}: \"{message[:120]}\"."
    return (f"Tapped Send for {name} ({number}) on {app}, but couldn't see the message in the chat yet. "
            "Check the phone.")
