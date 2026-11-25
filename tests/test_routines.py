"""Routines: defaults, running steps, allowlist, no network cut, saving."""

from __future__ import annotations

import pytest

from rose.common import ToolError
from rose.tools import apps, quick, routines, undo


@pytest.fixture
def ran(tmp_path, monkeypatch):
    monkeypatch.setattr(routines, "FILE", tmp_path / "routines.json")
    calls = []
    fake = {n: (lambda n=n: lambda **kw: calls.append((n, kw)) or "ok")() for n in routines.STEP_TOOLS}
    monkeypatch.setattr(routines, "_step_tools", lambda: fake)
    monkeypatch.setattr(routines, "SETTLE", 0)
    monkeypatch.setattr(undo, "UNDO_DIR", tmp_path / "undo")
    monkeypatch.setattr(undo, "JOURNAL", tmp_path / "undo" / "journal.jsonl")
    monkeypatch.setattr(undo, "_windows_snapshot", lambda: [])
    monkeypatch.setattr(quick, "quick_settings", lambda: {})
    return calls


def test_defaults_and_run(ran):
    assert {"work", "night", "break"} <= set(routines.list_routines())
    msg = routines.run_routine("Night mode")
    assert "Routine 'night'" in msg and ("set_quick_setting", {"setting": "night_light", "value": "on"}) in ran


def test_failures_are_reported_and_rest_runs(ran, monkeypatch):
    tools = routines._step_tools()

    def boom(**kw):
        raise ToolError("no player")

    tools["media_control"] = boom
    monkeypatch.setattr(routines, "_step_tools", lambda: tools)
    msg = routines.run_routine("break")
    assert "failed: media_control: no player" in msg and any(c[0] == "send_notification" for c in ran)


def test_save_validates(ran):
    with pytest.raises(ToolError, match="isn't allowed"):
        routines.save_routine("evil", '[{"tool": "power_control", "args": {}}]')
    with pytest.raises(ToolError, match="cut ROSE off"):
        routines.save_routine("offline", '[{"tool": "set_quick_setting", "args": {"setting": "wifi", "value": "off"}}]')
    assert "Saved" in routines.save_routine("gaming", '[{"tool": "open_app", "args": {"app": "steam"}}]', "games")
    with pytest.raises(ToolError, match="confirm=true"):
        routines.save_routine("gaming", '[{"tool": "open_app", "args": {"app": "lutris"}}]')
    routines.run_routine("gaming")
    assert ("open_app", {"app": "steam"}) in ran


def test_spoken_routine_names(ran, monkeypatch):
    from rose.tools import projects
    monkeypatch.setattr(routines, "_step_tools", lambda: {**{n: (lambda **kw: "ok") for n in routines.STEP_TOOLS}})
    assert "Routine 'work'" in routines.run_routine("fire up work mode")
    steps = [s["tool"] for s in routines._load()["work"]["steps"]]
    assert {"open_project", "open_web_app", "open_url"} <= set(steps)
    assert projects  # step tools import cleanly


def test_routine_is_undone_as_one_entry(ran, monkeypatch):
    state = {"do_not_disturb": False, "dark_style": True, "power_mode": "balanced"}
    wins = [{"id": "0x1", "title": "Terminal"}]
    monkeypatch.setattr(quick, "quick_settings", lambda: dict(state))
    monkeypatch.setattr(undo, "_windows_snapshot", lambda: list(wins))
    tools = routines._step_tools()

    def opener(**kw):
        wins.append({"id": "0x2", "title": "Visual Studio Code"})
        return "ok"

    tools["open_project"] = opener
    monkeypatch.setattr(routines, "_step_tools", lambda: tools)
    routines.run_routine("work mode")
    hist = undo.undo_history()
    assert len(hist) == 1 and hist[0]["action"] == "routine 'work'" and hist[0]["needs_confirm"]

    set_calls, closed = [], []
    monkeypatch.setattr(quick, "set_quick_setting", lambda s, v, confirm=False: set_calls.append((s, v)))
    monkeypatch.setattr(apps, "close_window", lambda w, confirm=False: closed.append((w, confirm)))
    with pytest.raises(ToolError, match="Visual Studio Code"):
        undo.undo_last()
    assert not set_calls and not closed  # nothing happened without confirm
    msg = undo.undo_last(confirm=True)
    assert set(set_calls) == {("do_not_disturb", "off"), ("dark_style", "on"), ("power_mode", "balanced")}
    assert closed == [("0x2", True)] and "closed 1 window" in msg
    assert undo.undo_history() == []


def test_routine_with_only_settings_needs_no_confirm(ran, monkeypatch):
    monkeypatch.setattr(quick, "quick_settings", lambda: {"night_light": False, "volume_percent": 55})
    routines.save_routine("calm", '[{"tool": "set_quick_setting", "args": {"setting": "night_light", "value": "on"}},'
                                  '{"tool": "set_quick_setting", "args": {"setting": "volume", "value": "20"}}]')
    routines.run_routine("calm")
    set_calls = []
    monkeypatch.setattr(quick, "set_quick_setting", lambda s, v, confirm=False: set_calls.append((s, v)))
    assert "night_light back to off" in undo.undo_last()
    assert set_calls == [("night_light", "off"), ("volume", "55")]


def test_routine_without_changes_is_not_journaled(ran):
    routines.run_routine("break")  # quick_settings() is empty, no windows appear
    assert undo.undo_history() == []


def test_launches_use_their_own_scope(monkeypatch):
    calls = []
    monkeypatch.setattr(routines, "launch_detached", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(apps, "launch_detached", lambda cmd, **kw: calls.append(cmd))
    routines.open_web_app("https://music.youtube.com")
    apps.open_url("example.com")
    assert calls[0][-1] == "--app=https://music.youtube.com" and calls[1] == ["xdg-open", "https://example.com"]
