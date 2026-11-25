"""rose.triggers: transitions only, never on the first tick, cooldowns, ask vs run."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from rose import triggers


@pytest.fixture
def world(monkeypatch):
    cfg = {}
    monkeypatch.setattr(triggers, "load_config", lambda: cfg)

    def settings(**kw):
        cfg.update(kw)

    w = {"monitors": 1, "wifi": "Home", "apps": ["code main.py"]}
    pushes, ran = [], []
    monkeypatch.setattr(triggers, "_prev", None)
    monkeypatch.setattr(triggers, "monitor_count", lambda: w["monitors"])
    monkeypatch.setattr(triggers, "wifi_ssid", lambda: w["wifi"])
    monkeypatch.setattr(triggers, "open_apps", lambda: w["apps"])
    monkeypatch.setattr(triggers, "push", lambda t, m="", **k: pushes.append((t, m, k)))
    from rose.tools import routines
    monkeypatch.setattr(routines, "run_routine", lambda name: ran.append(name) or f"Routine '{name}': 3 done.")
    return w, pushes, ran, settings


def test_default_monitor_trigger_asks(world):
    w, pushes, ran, _ = world
    t = datetime(2026, 11, 23, 9, 0)
    assert triggers.tick({}, t) == []  # first tick: observe only
    state = {}
    w["monitors"] = 2
    assert triggers.tick(state, t) == ["monitor_connected->work"]
    title, msg, kw = pushes[0]
    assert title == "Start work mode?" and kw["url"] == "https://debian.tail41061c.ts.net/?do=routine:work"
    assert ran == []
    w["monitors"] = 1
    triggers.tick(state, t)
    w["monitors"] = 2
    assert triggers.tick(state, t + timedelta(minutes=5)) == []  # cooldown
    w["monitors"] = 1
    triggers.tick(state, t)
    w["monitors"] = 2
    assert triggers.tick(state, t + timedelta(minutes=31)) == ["monitor_connected->work"]


def test_first_tick_never_fires_even_if_changed(world):
    w, pushes, _, _ = world
    w["monitors"] = 3
    triggers.tick({})
    assert pushes == []


def test_wifi_and_app_run_directly(world):
    w, pushes, ran, settings = world
    settings(context_triggers=[{"when": "wifi:Office", "routine": "work", "ask": False},
                               {"when": "app_opened:zoom", "routine": "Meeting", "ask": False, "cooldown_minutes": 0},
                               {"when": "bogus", "routine": "x"}], phone_app_url="https://p.example/")
    state = {}
    triggers.tick(state)
    w["wifi"] = "Office"
    w["apps"] = ["code main.py", "zoom meeting"]
    fired = triggers.tick(state)
    assert fired == ["wifi:Office->work", "app_opened:zoom->meeting"] and ran == ["work", "meeting"]
    assert pushes[0][0] == "🔁 Joined Wi-Fi Office" and "3 done" in pushes[0][1]
    assert triggers.tick(state) == []  # still there: no transition
    w["wifi"] = None  # unknown: no transition either way
    assert triggers.tick(state) == []


def test_ask_uses_phone_url(world):
    w, pushes, _, settings = world
    settings(context_triggers=[{"when": "monitor_disconnected", "routine": "break"}],
             phone_app_url="https://p.example/")
    w["monitors"] = 2
    triggers.tick({})
    w["monitors"] = None  # unknown (no X / xrandr): never a transition
    assert triggers.tick({}) == []
    w["monitors"] = 2
    triggers.tick({})
    w["monitors"] = 1
    assert triggers.tick({}) == ["monitor_disconnected->break"]
    assert pushes[-1][2]["url"] == "https://p.example/?do=routine:break"


def test_list_triggers(world, monkeypatch, tmp_path):
    from rose import watch
    monkeypatch.setattr(watch, "STATE", tmp_path / "s.json")
    watch._save(watch.STATE, {"triggers_last_fired": {"monitor_connected->work": "2026-11-23T09:00:00"}})
    rows = triggers.list_triggers()
    assert rows[0]["when"] == "monitor_connected" and rows[0]["last_fired"] == "2026-11-23T09:00:00"
    assert rows[0]["ask"] is True and "warning" not in rows[0]
