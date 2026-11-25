"""Today's calendar and email, for the Today view (ROSE Desktop), today_overview and meeting alerts.

No OAuth app needed:
* Calendar: one or more iCal (.ics) URLs, e.g. Google Calendar > Settings > your calendar >
  "Secret address in iCal format". Fetched every 10 min, parsed here (recurring events included).
* Email: IMAP with an app password (Gmail: imap.gmail.com + a Google app password). Read-only:
  ROSE opens the inbox with EXAMINE and fetches headers with BODY.PEEK, so nothing is marked read.
Both live in ~/rose/auth/agenda.json (0600), set with `rose agenda setup`. Email text is untrusted
data: the tool result carries the usual untrusted-content label.
"""

from __future__ import annotations

import email.header
import email.utils
import imaplib
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from rose.common import ToolError
from rose.config import ROSE_HOME, load_config

SETTINGS = ROSE_HOME / "auth" / "agenda.json"
CACHE_DIR = ROSE_HOME / "agenda_cache"
ICS_TTL = 600     # seconds a fetched calendar is reused
MAIL_TTL = 300
MAX_ICS_BYTES = 20_000_000
WEEKDAY = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


# ---------------- settings ----------------

def load_settings() -> dict:
    try:
        d = json.loads(SETTINGS.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(s: dict) -> None:
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(SETTINGS.parent, 0o700)
    fd = os.open(SETTINGS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(s, f, indent=2)


# ---------------- iCal parsing ----------------

def _unfold(text: str) -> list[str]:
    out: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        elif line:
            out.append(line)
    return out


def _prop(line: str) -> tuple[str, dict, str]:
    """'DTSTART;TZID=Europe/London:2026...' -> ('DTSTART', {'TZID': 'Europe/London'}, '2026...')."""
    head, _, value = line.partition(":")
    name, *params = head.split(";")
    return name.upper(), {k.upper(): v.strip('"') for k, _, v in (p.partition("=") for p in params)}, value


def _text(v: str) -> str:
    return v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _when(value: str, params: dict, local) -> tuple[datetime, bool]:
    """(local naive datetime, all_day)."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d"), True
    dt = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        dt = dt.replace(tzinfo=timezone.utc)
    elif params.get("TZID"):
        try:
            dt = dt.replace(tzinfo=ZoneInfo(params["TZID"]))
        except Exception:
            return dt, False  # unknown zone name (Outlook style): treat as local
    else:
        return dt, False  # floating time = local
    return dt.astimezone(local).replace(tzinfo=None), False


def parse_ics(text: str, local=None) -> list[dict]:
    """VEVENTs as dicts: uid, title, location, start, end (local naive datetimes), all_day, rrule, exdates,
    recurrence_id, cancelled."""
    local = local or datetime.now().astimezone().tzinfo
    events, cur = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {"exdates": set(), "title": "", "location": "", "rrule": "", "cancelled": False}
            continue
        if line == "END:VEVENT":
            if cur and cur.get("start"):
                if not cur.get("end"):
                    cur["end"] = cur["start"] + (timedelta(days=1) if cur["all_day"] else timedelta(0))
                events.append(cur)
            cur = None
            continue
        if cur is None:
            continue
        name, params, value = _prop(line)
        try:
            if name == "DTSTART":
                cur["start"], cur["all_day"] = _when(value, params, local)
            elif name == "DTEND":
                cur["end"] = _when(value, params, local)[0]
            elif name == "DURATION":
                cur["duration"] = value
            elif name == "SUMMARY":
                cur["title"] = _text(value)
            elif name == "LOCATION":
                cur["location"] = _text(value)
            elif name == "UID":
                cur["uid"] = value
            elif name == "RRULE":
                cur["rrule"] = value
            elif name == "EXDATE":
                for v in value.split(","):
                    cur["exdates"].add(_when(v, params, local)[0])
            elif name == "RECURRENCE-ID":
                cur["recurrence_id"] = _when(value, params, local)[0]
            elif name == "STATUS":
                cur["cancelled"] = value.strip().upper() == "CANCELLED"
        except ValueError:
            continue
    for e in events:
        m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?", e.pop("duration", "") or "")
        if m and e["end"] == e["start"]:
            d, h, mi = (int(x or 0) for x in m.groups())
            e["end"] = e["start"] + timedelta(days=d, hours=h, minutes=mi)
    return events


def _rule(rrule: str) -> dict:
    return {k.upper(): v for k, _, v in (p.partition("=") for p in rrule.split(";") if p)}


def _add_months(d: datetime, n: int) -> datetime | None:
    y, m = divmod(d.month - 1 + n, 12)
    try:
        return d.replace(year=d.year + y, month=m + 1)
    except ValueError:  # the 31st in a short month: RFC 5545 skips it
        return None


def _nth_weekday(month_start: datetime, spec: str) -> datetime | None:
    """'2TU' -> 2nd Tuesday of that month, '-1FR' -> last Friday (time of day kept from month_start)."""
    m = re.fullmatch(r"([+-]?\d+)([A-Z]{2})", spec.strip())
    if not m or m.group(2) not in WEEKDAY:
        return None
    n, wd = int(m.group(1)), WEEKDAY[m.group(2)]
    if n > 0:
        d = month_start + timedelta(days=(wd - month_start.weekday()) % 7 + 7 * (n - 1))
    else:
        nxt = _add_months(month_start, 1)
        last = nxt - timedelta(days=1)
        d = last - timedelta(days=(last.weekday() - wd) % 7 + 7 * (-n - 1))
    return d if d.month == month_start.month else None


def occurrences(ev: dict, day: date, local=None) -> list[datetime]:
    """Start times of `ev` that fall on `day` (recurring events expanded; EXDATEs removed)."""
    start = ev["start"]
    span = ev["end"] - start
    lo, hi = datetime.combine(day, datetime.min.time()), datetime.combine(day, datetime.min.time()) + timedelta(days=1)

    def hits(s: datetime) -> bool:
        e = s + span
        return (s < hi and e > lo) if span else lo <= s < hi

    if not ev.get("rrule"):
        return [start] if hits(start) else []
    r = _rule(ev["rrule"])
    freq, step = r.get("FREQ", ""), max(1, int(r.get("INTERVAL", "1") or 1))
    count = int(r["COUNT"]) if r.get("COUNT", "").isdigit() else None
    until = None
    if r.get("UNTIL"):
        try:
            until = _when(r["UNTIL"], {}, local or datetime.now().astimezone().tzinfo)[0]
            if len(r["UNTIL"]) == 8:
                until += timedelta(days=1) - timedelta(seconds=1)
        except ValueError:
            until = None
    byday = [WEEKDAY[d[-2:]] for d in r.get("BYDAY", "").split(",") if d[-2:] in WEEKDAY]
    out, n, i = [], 0, 0
    while i < 20000:  # hard stop: a daily rule from 1970 is ~20k steps
        if freq == "DAILY":
            cands = [start + timedelta(days=i * step)]
        elif freq == "WEEKLY":
            week0 = start - timedelta(days=start.weekday()) + timedelta(weeks=i * step)
            cands = sorted(week0 + timedelta(days=wd) for wd in (byday or [start.weekday()]))
            cands = [c for c in cands if c >= start]
        elif freq == "MONTHLY" and re.search(r"[+-]?\d+[A-Z]{2}", r.get("BYDAY", "")):
            first = start.replace(day=1)
            month = _add_months(first, i * step)
            cands = sorted(c for d in r["BYDAY"].split(",") if (c := _nth_weekday(month, d)) and c >= start)
        elif freq == "MONTHLY":
            c = _add_months(start, i * step)
            cands = [c] if c else []
        elif freq == "YEARLY":
            c = _add_months(start, 12 * i * step)
            cands = [c] if c else []
        else:
            return [start] if hits(start) else []
        i += 1
        stop = False
        for c in cands:
            if (until and c > until) or (count is not None and n >= count):
                stop = True
                break
            n += 1
            if c >= hi:
                stop = True
                break
            if c not in ev["exdates"] and hits(c):
                out.append(c)
        if stop:
            break
    return out


def events_on(events: list[dict], day: date) -> list[dict]:
    """Every event instance on `day`, sorted, with moved/cancelled single instances applied."""
    overrides = {(e.get("uid"), e["recurrence_id"]) for e in events if e.get("recurrence_id")}
    out = []
    for e in events:
        for s in occurrences(e, day):
            if not e.get("recurrence_id") and (e.get("uid"), s) in overrides:
                continue  # this instance was moved or changed: the override event stands in for it
            if e["cancelled"]:
                continue
            end = s + (e["end"] - e["start"])
            out.append({"title": e["title"] or "(no title)", "location": e["location"], "all_day": e["all_day"],
                        "start": s, "end": end})
    out.sort(key=lambda x: (not x["all_day"], x["start"]))
    return out


# ---------------- fetching ----------------

def _cache_path(key: str) -> Path:
    import hashlib
    return CACHE_DIR / (hashlib.sha256(key.encode()).hexdigest()[:20] + ".json")


def _cached(key: str, ttl: float, fetch, refresh: bool = False):
    p = _cache_path(key)
    if not refresh:
        try:
            d = json.loads(p.read_text())
            if time.time() - d["at"] < ttl:
                return d["data"]
        except (OSError, ValueError, KeyError):
            pass
    data = fetch()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CACHE_DIR, 0o700)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"at": time.time(), "data": data}, default=str))
    tmp.replace(p)
    return data


def _fetch_ics(url: str) -> str:
    import httpx
    url = url.strip()
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if not url.startswith("https://"):
        raise ToolError("Calendar URLs must be https:// (or webcal://).")
    with httpx.stream("GET", url, timeout=20, follow_redirects=True) as r:
        r.raise_for_status()
        buf = b""
        for chunk in r.iter_bytes():
            buf += chunk
            if len(buf) > MAX_ICS_BYTES:
                raise ToolError("Calendar feed is too big.")
    return buf.decode("utf-8", errors="replace")


def calendar_day(day: date | None = None, refresh: bool = False) -> dict:
    """{'events': [...], 'errors': [...], 'configured': bool} for `day` (default today)."""
    day = day or date.today()
    urls = [u for u in load_settings().get("calendar_urls", []) if isinstance(u, str) and u.strip()]
    if not urls:
        return {"configured": False, "events": [], "errors": []}
    events, errors = [], []
    for n, url in enumerate(urls, 1):
        try:
            text = _cached("ics:" + url, ICS_TTL, lambda u=url: _fetch_ics(u), refresh)
            for e in events_on(parse_ics(text), day):
                e["calendar"] = n
                events.append(e)
        except Exception as e:  # one broken feed must not hide the others
            errors.append(f"calendar {n}: {type(e).__name__}: {str(e)[:120]}")
    events.sort(key=lambda x: (not x["all_day"], x["start"]))
    return {"configured": True, "events": events, "errors": errors}


def _decode(h: str | None) -> str:
    if not h:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(h))).replace("\n", " ").strip()
    except Exception:
        return h.strip()


def _fetch_mail(s: dict, limit: int) -> list[dict]:
    host = s.get("imap_host") or "imap.gmail.com"
    with imaplib.IMAP4_SSL(host, int(s.get("imap_port") or 993), timeout=20) as m:
        m.login(s["imap_user"], s["imap_password"])
        m.select("INBOX", readonly=True)  # EXAMINE: never changes \Seen
        since = date.today().strftime("%d-%b-%Y")
        typ, data = m.search(None, "SINCE", since)
        ids = (data[0] or b"").split()[-limit:] if typ == "OK" else []
        out = []
        for mid in reversed(ids):
            typ, msg = m.fetch(mid, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if typ != "OK" or not msg or not isinstance(msg[0], tuple):
                continue
            flags = msg[0][0].decode(errors="replace")
            hdr = email.message_from_bytes(msg[0][1])
            try:
                when = email.utils.parsedate_to_datetime(hdr["Date"]).astimezone().strftime("%H:%M")
            except Exception:
                when = ""
            name, addr = email.utils.parseaddr(_decode(hdr["From"]))
            out.append({"from": name or addr, "address": addr, "subject": _decode(hdr["Subject"]) or "(no subject)",
                        "time": when, "unread": "\\Seen" not in flags})
        return out


def email_today(refresh: bool = False, limit: int | None = None) -> dict:
    s = load_settings()
    limit = int(limit or load_config().get("agenda_email_limit", 15))
    if not (s.get("imap_user") and s.get("imap_password")):
        return {"configured": False, "messages": [], "error": ""}
    try:
        msgs = _cached(f"mail:{s['imap_user']}:{limit}", MAIL_TTL, lambda: _fetch_mail(s, limit), refresh)
        return {"configured": True, "messages": msgs, "error": ""}
    except Exception as e:
        return {"configured": True, "messages": [], "error": f"{type(e).__name__}: {str(e)[:160]}"}


# ---------------- views ----------------

def _fmt_event(e: dict) -> dict:
    s, en = e["start"], e["end"]
    s = s if isinstance(s, datetime) else datetime.fromisoformat(s)
    en = en if isinstance(en, datetime) else datetime.fromisoformat(en)
    return {"time": "all day" if e["all_day"] else f"{s:%H:%M}-{en:%H:%M}", "title": e["title"],
            "location": e.get("location", ""), "all_day": e["all_day"],
            "start": s.isoformat(timespec="minutes"), "end": en.isoformat(timespec="minutes")}


def today(refresh: bool = False) -> dict:
    """Plain-data view for ROSE Desktop's Today tab and today_overview."""
    cal, mail = calendar_day(refresh=refresh), email_today(refresh=refresh)
    now = datetime.now()
    events = [_fmt_event(e) for e in cal["events"]]
    nxt = next((e for e in events if not e["all_day"] and datetime.fromisoformat(e["end"]) > now), None)
    out = {"date": f"{now:%a %d %b %Y}", "now": f"{now:%H:%M}", "events": events, "next": nxt,
           "email": mail["messages"], "unread": sum(m["unread"] for m in mail["messages"])}
    notes = []
    if not cal["configured"]:
        notes.append("Calendar not set up: run `rose agenda setup` (paste your calendar's secret iCal URL).")
    if not mail["configured"]:
        notes.append("Email not set up: run `rose agenda setup` (IMAP + app password).")
    notes += cal["errors"] + ([f"email: {mail['error']}"] if mail["error"] else [])
    if notes:
        out["notes"] = notes
    return out


def today_overview(refresh: bool = False) -> dict:
    """Today's calendar events (with the next one) and today's inbox emails (sender, subject, time, unread).
    refresh=true skips the 5-10 min cache. Set up with `rose agenda setup`."""
    return today(refresh=bool(refresh))


def upcoming(within_minutes: float, now: datetime | None = None) -> list[dict]:
    """Timed events starting in (now, now + within_minutes] (for meeting alerts; uses the cache)."""
    now = now or datetime.now()
    try:
        cal = calendar_day(now.date())
    except Exception:
        return []
    return [e for e in cal["events"] if not e["all_day"] and now < e["start"] <= now + timedelta(minutes=within_minutes)]


# ---------------- CLI ----------------

def cli(argv: list[str]) -> int:
    import getpass
    action = argv[0] if argv else "status"
    s = load_settings()
    if action == "setup":
        print("Calendar: paste secret iCal URLs (Google Calendar > Settings > calendar > 'Secret address in iCal "
              "format'), one per line, empty line to finish. Leave empty to keep the current ones.")
        urls = []
        while True:
            u = input("  iCal URL: ").strip()
            if not u:
                break
            urls.append(u)
        if urls:
            s["calendar_urls"] = urls
        user = input(f"Email (IMAP) address [{s.get('imap_user', '')}] (empty = keep/skip): ").strip()
        if user:
            s["imap_user"] = user
            s["imap_host"] = input("  IMAP host [imap.gmail.com]: ").strip() or "imap.gmail.com"
            s["imap_password"] = getpass.getpass("  App password (Gmail: myaccount.google.com/apppasswords): ")
        save_settings(s)
        print(f"Saved to {SETTINGS} (0600).")
        action = "test"
    if action == "off":
        SETTINGS.unlink(missing_ok=True)
        print("Calendar and email removed.")
        return 0
    if action in ("test", "status"):
        t = today(refresh=action == "test")
        print(f"{t['date']}: {len(t['events'])} events, {len(t['email'])} emails today ({t['unread']} unread)")
        for e in t["events"]:
            print(f"  {e['time']:13} {e['title']}")
        for m in t["email"][:5]:
            print(f"  {'*' if m['unread'] else ' '} {m['time']:5} {m['from'][:24]:24} {m['subject'][:60]}")
        for n in t.get("notes", []):
            print("  note:", n)
        return 0
    print("usage: rose agenda setup | status | test | off")
    return 2
