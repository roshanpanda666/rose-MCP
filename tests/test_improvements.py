"""Tool profiles, selftest plan, agenda (iCal + IMAP), failure report, proactive alerts, phone-check runner."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from rose import agenda, failures, phonecheck, proactive, profiles, selftest, server
from rose.common import ToolError

ALL = [fn.__name__ for fn, _ in server._tool_table(include_optional=True)]


# ---------------- profiles ----------------

def _server():
    m = MCPServer(name="rose")
    server.register_tools(m)
    return m


def test_full_profile_is_default_and_lists_everything(monkeypatch):
    monkeypatch.delenv("ROSE_PROFILE", raising=False)
    names = [t.name for t in asyncio.run(_server().list_tools())]
    assert names == [fn.__name__ for fn, _ in server._tool_table()]  # flagged tools only when switched on


@pytest.mark.parametrize("prof", ["dev", "phone", "desktop"])
def test_profile_is_a_smaller_ordered_subset(monkeypatch, prof):
    monkeypatch.setenv("ROSE_PROFILE", prof)
    names = [t.name for t in asyncio.run(_server().list_tools())]
    assert 20 < len(names) < len(ALL)
    assert names == [n for n in ALL if n in set(names)]  # same order as the full list (prompt cache)
    assert "check_approval" in names and "today_overview" in names


def test_profile_contents():
    dev, phone, desk = (profiles.expand(p, ALL) for p in ("dev", "phone", "desktop"))
    assert {"git_status", "hand_to_claude", "docker"} <= dev and "phone_call" not in dev
    assert {"phone_call", "phone_lost_mode", "send_to_phone"} <= phone and "git_commit" not in phone
    assert {"ui_click", "browser_open", "wifi_connect"} <= desk and "phone_call" not in desk


def test_every_profile_pattern_matches_a_real_tool():
    for name, pats in {**profiles.BUILTIN, "core": profiles.CORE}.items():
        for p in pats:
            assert any(re.fullmatch(p.replace("*", ".*"), t) for t in ALL), (name, p)


def test_call_outside_profile_is_refused(monkeypatch):
    monkeypatch.setenv("ROSE_PROFILE", "dev")
    with pytest.raises(ToolError, match="not in this client's tool profile 'dev'"):
        asyncio.run(_server().call_tool("phone_call", {"who": "x"}))


def test_client_profiles_and_custom_profile(settings, monkeypatch):
    monkeypatch.delenv("ROSE_PROFILE", raising=False)
    settings(client_profiles={"Forge": "mini"}, tool_profiles={"mini": ["git_*"]})
    assert profiles.current_profile() == "mini"  # tests have no auth token, so the caller is "Forge"
    names = {t.name for t in asyncio.run(_server().list_tools())}
    assert "git_status" in names and "docker" not in names and "memory_save" in names


def test_unknown_profile_errors_instead_of_falling_back():
    with pytest.raises(ToolError, match="Unknown tool profile"):
        profiles.expand("nope", ALL)


# ---------------- selftest plan ----------------

def test_selftest_plan_covers_every_tool():
    missing = set(ALL) - set(selftest.PLAN)
    assert not missing, f"add these tools to rose/selftest.py PLAN: {sorted(missing)}"
    assert not set(selftest.PLAN) - set(ALL), "PLAN lists tools that no longer exist"


def test_automatic_plan_entries_are_really_checked():
    """A tool planned as default/full/phone must actually be called in the suite."""
    src = Path(selftest.__file__).read_text().split("# ---------------- coverage plan")[0]
    called = set(re.findall(r'"(\w+)"', src))
    auto = [t for t, how in selftest.PLAN.items() if how.split(" ")[0] in ("default", "full", "phone")]
    assert not [t for t in auto if t not in called]


def test_phone_check_steps_exist():
    for how in selftest.PLAN.values():
        if how.startswith("phone-check:"):
            assert how.split(":", 1)[1] in phonecheck.Checker.STEPS


# ---------------- agenda: iCal ----------------

ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:a
DTSTART:20261126T093000
DTEND:20261126T100000
SUMMARY:Stand-up\\, team
LOCATION:Meet
END:VEVENT
BEGIN:VEVENT
UID:weekly
DTSTART:20261102T140000
DTEND:20261102T143000
RRULE:FREQ=WEEKLY;BYDAY=MO,TH
EXDATE:20261119T140000
SUMMARY:Sync
END:VEVENT
BEGIN:VEVENT
UID:daily
DTSTART:20261120T080000
DURATION:PT15M
RRULE:FREQ=DAILY;COUNT=3
SUMMARY:Short
END:VEVENT
BEGIN:VEVENT
UID:monthly
DTSTART:20260908T110000
DTEND:20260908T120000
RRULE:FREQ=MONTHLY;BYDAY=2TU
SUMMARY:Monthly review
END:VEVENT
BEGIN:VEVENT
UID:allday
DTSTART;VALUE=DATE:20261126
DTEND;VALUE=DATE:20261127
SUMMARY:Holiday
END:VEVENT
BEGIN:VEVENT
UID:gone
DTSTART:20261126T150000
DTEND:20261126T160000
STATUS:CANCELLED
SUMMARY:Cancelled one
END:VEVENT
BEGIN:VEVENT
UID:weekly
RECURRENCE-ID:20261126T140000
DTSTART:20261126T170000
DTEND:20261126T173000
SUMMARY:Sync (moved)
END:VEVENT
END:VCALENDAR
"""


def _titles(day):
    return [(e["title"], e["start"].strftime("%H:%M")) for e in agenda.events_on(agenda.parse_ics(ICS), day)]


def test_ics_single_allday_cancelled_and_moved_instance():
    t = _titles(date(2026, 11, 26))  # a Thursday
    assert t[0] == ("Holiday", "00:00")  # all-day first
    assert ("Stand-up, team", "09:30") in t
    assert ("Sync (moved)", "17:00") in t and ("Sync", "14:00") not in t
    assert not any("Cancelled" in x for x, _ in t)


def test_ics_weekly_exdate_daily_count_monthly_byday():
    assert ("Sync", "14:00") in _titles(date(2026, 11, 23))   # Monday
    assert ("Sync", "14:00") not in _titles(date(2026, 11, 19))  # EXDATE
    assert ("Sync", "14:00") not in _titles(date(2026, 11, 24))  # Tuesday
    assert ("Short", "08:00") in _titles(date(2026, 11, 22))
    assert ("Short", "08:00") not in _titles(date(2026, 11, 23))  # COUNT=3 ended
    assert ("Monthly review", "11:00") in _titles(date(2026, 11, 10))  # 2nd Tuesday of November
    assert ("Monthly review", "11:00") not in _titles(date(2026, 11, 17))


def test_ics_utc_converted_to_local():
    ics = "BEGIN:VEVENT\nDTSTART:20261126T120000Z\nDTEND:20261126T130000Z\nSUMMARY:UTC\nEND:VEVENT\n"
    from datetime import timezone as tz
    ev = agenda.parse_ics(ics, local=tz(timedelta(hours=5, minutes=30)))[0]
    assert ev["start"] == datetime(2026, 11, 26, 17, 30)


def test_today_without_setup_explains_how():
    t = agenda.today()
    assert t["events"] == [] and t["email"] == []
    assert any("rose agenda setup" in n for n in t["notes"])


def test_calendar_feed_and_upcoming(monkeypatch):
    agenda.save_settings({"calendar_urls": ["https://example.invalid/cal.ics"]})
    try:
        now = datetime.now().replace(second=0, microsecond=0)
        start = now + timedelta(minutes=5)
        ics = (f"BEGIN:VEVENT\nDTSTART:{start:%Y%m%dT%H%M%S}\nDTEND:{start + timedelta(minutes=30):%Y%m%dT%H%M%S}\n"
               "SUMMARY:Design review\nEND:VEVENT\n")
        monkeypatch.setattr(agenda, "_fetch_ics", lambda url: ics)
        assert [e["title"] for e in agenda.upcoming(10, now)] == ["Design review"]
        assert agenda.upcoming(3, now) == []
        if start.date() == now.date():
            assert agenda.today(refresh=True)["next"]["title"] == "Design review"
    finally:
        agenda.SETTINGS.unlink(missing_ok=True)


def test_email_today_reads_imap_without_marking_read(monkeypatch):
    calls = {}

    class FakeIMAP:
        def __init__(self, host, port, timeout):
            calls["host"] = host

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, u, p):
            calls["login"] = u

        def select(self, box, readonly=False):
            calls["readonly"] = readonly

        def search(self, _, *crit):
            return "OK", [b"1 2"]

        def fetch(self, mid, what):
            calls.setdefault("fetch", []).append(what)
            seen = "\\Seen" if mid == b"1" else ""
            hdr = (f"From: Alice <a@x.com>\r\nSubject: Hello {mid.decode()}\r\n"
                   "Date: Thu, 26 Nov 2026 09:15:00 +0000\r\n\r\n").encode()
            return "OK", [(f"{mid.decode()} (FLAGS ({seen}))".encode(), hdr)]

    monkeypatch.setattr(agenda.imaplib, "IMAP4_SSL", FakeIMAP)
    agenda.save_settings({"imap_user": "me@x.com", "imap_password": "pw"})
    try:
        r = agenda.email_today(refresh=True)
    finally:
        agenda.SETTINGS.unlink(missing_ok=True)
    assert calls["readonly"] is True and all("PEEK" in f for f in calls["fetch"])
    assert [m["subject"] for m in r["messages"]] == ["Hello 2", "Hello 1"]  # newest first
    assert [m["unread"] for m in r["messages"]] == [True, False]
    assert oct(agenda.SETTINGS.parent.stat().st_mode)[-3:] == "700"


# ---------------- failure report ----------------

def test_failure_report_counts_rose_and_phone_failures(tmp_path, settings):
    from rose.config import LOG_DIR
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    log = LOG_DIR / f"{now:%Y-%m-%d}.jsonl"
    rows = [{"ts": now.isoformat(timespec="seconds"), "caller": "Phone app", "tool": "press_keys", "ok": False,
             "error": "ToolError: Unknown key 'Enterr'", "args": {}},
            {"ts": now.isoformat(timespec="seconds"), "caller": "Forge", "tool": "press_keys", "ok": False,
             "error": "ToolError: Unknown key 'Foo'", "args": {}},
            {"ts": now.isoformat(timespec="seconds"), "caller": "Forge", "tool": "list_dir", "ok": True},
            {"ts": (now - timedelta(days=30)).isoformat(timespec="seconds"), "tool": "old", "ok": False}]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    app = tmp_path / "backend"
    (app / "logs").mkdir(parents=True)
    (app / ".env").write_text("")
    (app / "logs" / "replies.jsonl").write_text(
        json.dumps({"ts": now.isoformat(timespec="seconds"), "kind": "give_up_dropped", "user": "turn on hotspot",
                    "text": "I can't control the hotspot."}) + "\n"
        + json.dumps({"ts": now.isoformat(timespec="seconds"), "kind": "cant_reply", "user": "x",
                      "text": "Sorry, I'm unable to do that."}) + "\n")
    (app / "logs" / "tool-calls.jsonl").write_text(
        json.dumps({"ts": now.isoformat(timespec="seconds"), "tool": "app_ui", "ok": False}) + "\n")
    settings(companion_env_file=str(app / ".env"))
    r = failures.report(7, save=True)
    assert r["failed_calls"] == 3 and r["rose_calls"] == 3
    assert r["top"][0] == {"tool": "press_keys", "failures": 2, "error": "ToolError: Unknown key '…'"}
    assert r["cant_replies"] == 1 and r["cant_dropped"] == 1
    assert "turn on hotspot" in r["markdown"] and Path(r["saved"]).exists()


def test_weekly_due_once_per_week(settings):
    settings(failure_report_day="thursday", failure_report_time="09:00")  # [:3] is used; different size busts the config cache
    st: dict = {}
    thu = datetime(2026, 11, 26, 8, 59)
    assert not failures.weekly_due(st, thu)
    assert failures.weekly_due(st, thu.replace(hour=9, minute=1))
    assert not failures.weekly_due(st, thu.replace(hour=15))
    assert not failures.weekly_due(st, datetime(2026, 11, 27, 10))


# ---------------- proactive ----------------

@pytest.fixture
def pushed(monkeypatch):
    out = []
    monkeypatch.setattr(proactive, "push", lambda title, msg="", **k: out.append((title, msg)))
    monkeypatch.setattr(proactive, "_say", lambda text: out.append(("say", text)))
    return out


def test_quiet_hours():
    assert proactive.in_quiet_hours(datetime(2026, 1, 1, 23, 0), "22:00-08:00")
    assert proactive.in_quiet_hours(datetime(2026, 1, 1, 7, 59), "22:00-08:00")
    assert not proactive.in_quiet_hours(datetime(2026, 1, 1, 12, 0), "22:00-08:00")
    assert proactive.in_quiet_hours(datetime(2026, 1, 1, 13, 0), "12:00-14:00")
    assert not proactive.in_quiet_hours(datetime(2026, 1, 1, 13, 0), "")


def test_meeting_announced_once(monkeypatch, pushed):
    now = datetime(2026, 11, 26, 9, 50)
    ev = {"title": "Stand-up", "location": "Meet", "all_day": False, "start": datetime(2026, 11, 26, 10, 0),
          "end": datetime(2026, 11, 26, 10, 15)}
    monkeypatch.setattr(agenda, "upcoming", lambda mins, now=None: [ev])
    st: dict = {}
    assert len(proactive.tick_meetings(st, now)) == 1
    assert proactive.tick_meetings(st, now) == []
    assert pushed[0][0] == "📅 Stand-up in 10 min"


def test_break_reminder_after_continuous_activity(pushed, settings):
    settings(break_reminder_minutes=120)
    st: dict = {}
    t0 = 1_000_000.0
    assert not proactive.tick_break(st, idle=0, now=t0)
    assert not proactive.tick_break(st, idle=0, now=t0 + 119 * 60)
    assert proactive.tick_break(st, idle=0, now=t0 + 121 * 60)
    assert "2 hours" in pushed[0][1]
    assert not proactive.tick_break(st, idle=0, now=t0 + 125 * 60)  # next one only after another stretch
    assert not proactive.tick_break(st, idle=600, now=t0 + 130 * 60)  # a real break resets
    assert st["active_since"] is None


def test_phone_battery_alert(monkeypatch, pushed):
    import shutil

    from rose.tools import phone, phone_control
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/" + b)
    monkeypatch.setattr(phone, "_read_last", lambda: {"_watch_via": "wifi"})
    level = {"v": 12, "s": "discharging"}
    monkeypatch.setattr(phone_control, "phone_status", lambda: {"battery": {"level": level["v"], "state": level["s"]}})
    st: dict = {}
    assert proactive.tick_phone_battery(st)
    assert not proactive.tick_phone_battery(st)  # told once
    level.update(v=60, s="charging")
    assert not proactive.tick_phone_battery(st)
    level.update(v=10, s="discharging")
    assert proactive.tick_phone_battery(st)


def test_ci_runs_seed_then_alert_on_new_failure(monkeypatch, pushed):
    import shutil

    from rose import watch
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/" + b)
    monkeypatch.setattr(watch, "_gh_login", lambda st: "me")
    pushed_at = datetime.now().astimezone().isoformat()
    runs = {"workflow_runs": [{"id": 5, "status": "completed", "conclusion": "success"}]}

    def gh(path):
        return [{"full_name": "me/rose-MCP", "pushed_at": pushed_at}] if path.startswith("user/repos") else runs
    monkeypatch.setattr(proactive, "_gh", gh)
    st: dict = {}
    assert proactive.tick_ci_runs(st) == []  # first look seeds silently
    runs["workflow_runs"].insert(0, {"id": 6, "status": "completed", "conclusion": "failure", "name": "CI",
                                     "head_branch": "main", "html_url": "u"})
    assert [r["id"] for r in proactive.tick_ci_runs(st)] == [6]
    assert proactive.tick_ci_runs(st) == []
    assert pushed[0][0] == "❌ CI failed: rose-MCP"


def test_proactive_off_does_nothing(settings, monkeypatch):
    settings(proactive_enabled=False)
    monkeypatch.setattr(proactive, "tick_break", lambda st: pytest.fail("ran"))
    proactive.tick({})


# ---------------- phone-check runner ----------------

class FakePhone:
    def __init__(self, tmp):
        self.calls, self.tmp, self.via = [], tmp, "wifi"
        self.reboots = 0

    def link_status(self, reconnect=False):
        if self.reboots:
            self.reboots -= 1
            return {"via": "offline"}
        return {"via": self.via}

    def phone_status(self):
        return {"battery": {"level": 80, "state": "charging"}}

    def phone_location(self):
        return {"maps": "https://maps", "accuracy_m": 12}

    def phone_contacts(self, q):
        return []

    def phone_call(self, who, confirm=False):
        assert confirm
        self.calls.append(("call", who))
        return "Calling"

    def phone_call_control(self, action):
        self.calls.append(("cc", action))
        return "offhook" if action == "status" else "Ended"

    def phone_message(self, who, text, app="whatsapp", confirm=False):
        assert confirm
        self.calls.append(("msg", who, app))
        return "Sent"

    def phone_lost_mode(self, message="", siren=True, confirm=False):
        return ["Location: ok\nFront photo: ok\nScreen locked."]

    def phone_stop_sound(self):
        return "Stopped"

    def phone_power(self, action, confirm=False):
        assert confirm
        self.calls.append(("power", action))
        self.reboots = 2
        return "rebooting"

    class _Mic:
        level, running = -40.0, False

        def start(self):
            self.running = True

        def stop(self):
            self.running = False

    def mic_listen(self):
        return self._Mic()

    def play_on_phone(self, mp3, volume):
        return "Played"

    def to_mp3(self, path):
        return b"mp3"

    def out_path(self, name):
        return self.tmp / name

    def stamp(self):
        return "now"


def _checker(tmp_path, answers):
    it = iter(answers)
    fake = FakePhone(tmp_path)
    c = phonecheck.Checker(tools=fake, ask=lambda q: next(it, ""), say=lambda s: None, sleep=lambda s: None,
                           record=lambda path, secs: Path(path).write_bytes(b"\0" * 20000))
    return c, fake


def test_phone_check_full_run(tmp_path):
    answers = ["+911234567890", "y",           # call: number, did it dial
               "me", "sms", "y",              # message
               "y", "y",                      # lost mode: run, worked
               "y", "y",                      # listen live: start, heard
               "y", "y",                      # hold to talk: run, heard
               "y"]                           # reboot
    c, fake = _checker(tmp_path, answers)
    res = c.run()
    assert {k: v["status"] for k, v in res.items()} == {
        "link": "pass", "status": "pass", "location": "pass", "call": "pass", "message": "pass",
        "lost_mode": "pass", "listen_live": "pass", "hold_to_talk": "pass", "reboot": "pass"}
    assert ("cc", "end") in fake.calls and ("msg", "me", "sms") in fake.calls and ("power", "reboot") in fake.calls
    assert "poweroff" not in res  # only with include_poweroff


def test_phone_check_declines_and_failures(tmp_path):
    c, fake = _checker(tmp_path, ["", "", "n", "n", "n", "n"])
    fake.phone_location = lambda: (_ for _ in ()).throw(RuntimeError("no fix"))
    res = c.run()
    assert res["location"]["status"] == "fail" and "no fix" in res["location"]["detail"]
    assert all(res[s]["status"] == "skip" for s in ("call", "message", "lost_mode", "listen_live", "hold_to_talk",
                                                    "reboot"))


def test_phone_check_stops_without_link(tmp_path):
    c, fake = _checker(tmp_path, [])
    fake.via = "offline"
    assert c.run() == {"link": {**c.results["link"]}} and c.results["link"]["status"] == "fail"


def test_phone_check_only_steps(tmp_path):
    c, _ = _checker(tmp_path, ["y", "y"])
    res = c.run(["listen_live"])
    assert set(res) == {"link", "listen_live"}
