"""rose.activity: sampler (changes / heartbeat / idle), summaries, standup, daily push."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from rose import activity


@pytest.fixture
def act(tmp_path, monkeypatch):
    monkeypatch.setattr(activity, "ACTIVITY_DIR", tmp_path / "activity")
    monkeypatch.setattr(activity, "CLAUDE_EVENTS", tmp_path / "claude_events.jsonl")
    monkeypatch.setattr(activity, "_last", {})
    monkeypatch.setattr(activity, "_cache", {"proj_at": 9e12, "proj": [
        {"name": "rose-MCP", "path": "/home/u/0x/projects/rose-MCP"},
        {"name": "shop", "path": "/home/u/0x/work/shop"}]})
    monkeypatch.setattr(activity, "is_locked", lambda: False)
    env = {"idle": 0.0, "win": {"app": "Code", "title": "main.py - rose-MCP - Visual Studio Code", "pid": 1}}
    monkeypatch.setattr(activity, "idle_seconds", lambda: env["idle"])
    import rose.tools.apps
    monkeypatch.setattr(rose.tools.apps, "active_window", lambda: env["win"])
    return env, tmp_path


def test_vscode_folder():
    assert activity.vscode_folder("● main.py - rose-MCP - Visual Studio Code") == "rose-MCP"
    assert activity.vscode_folder("rose-MCP - Visual Studio Code") == "rose-MCP"
    assert activity.vscode_folder("x.py - shop [SSH: box] - Visual Studio Code") == "shop"
    assert activity.vscode_folder("GitHub - Brave") == ""


def test_project_of(act):
    assert activity.project_of({"title": "roshan@debian: ~/0x/work/shop/src", "app": "x"}) == "shop"  # by name
    assert activity.project_of({"title": "u@h: /home/u/0x/work/shop/src", "app": "x"}) == "shop"
    assert activity.project_of({"title": "Pull requests · me/rose-MCP - Brave", "app": "Brave"}) == "rose-MCP"
    assert activity.project_of({"title": "YouTube - Brave", "app": "Brave"}) == ""


def test_sampler_writes_on_change_heartbeat_and_idle(act):
    env, _ = act
    t0 = datetime(2026, 11, 23, 10, 0).timestamp()
    assert activity.sample(t0)["project"] == "rose-MCP"
    assert activity.sample(t0 + 30) is None  # unchanged
    assert activity.sample(t0 + 301) is not None  # heartbeat
    env["win"] = {"app": "Brave-browser", "title": "YouTube - Brave"}
    assert activity.sample(t0 + 330)["app"] == "Brave-browser"
    env["idle"] = 400
    row = activity.sample(t0 + 700)
    assert row["idle"] and row["ts"] == datetime.fromtimestamp(t0 + 330).isoformat(timespec="seconds")
    assert activity.sample(t0 + 730) is None
    env["idle"] = 1
    assert activity.sample(t0 + 900)["app"] == "Brave-browser"


def _write(tmp, day, rows):
    d = tmp / "activity"
    d.mkdir(exist_ok=True)
    (d / f"{day}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_summary_minutes(act):
    _, tmp = act
    _write(tmp, "2026-11-20", [
        {"ts": "2026-11-20T09:00:00", "app": "Code", "project": "rose-MCP", "title": "a"},
        {"ts": "2026-11-20T09:05:00", "app": "Code", "project": "rose-MCP", "title": "a"},
        {"ts": "2026-11-20T09:10:00", "app": "Brave", "project": "", "title": "b"},
        {"ts": "2026-11-20T09:20:00", "idle": True},  # brave row: capped at 6 min (no rows in between)
        {"ts": "2026-11-20T13:00:00", "app": "Code", "project": "shop", "title": "c"},
        {"ts": "2026-11-20T13:03:00", "idle": True},
        "garbage",
    ])
    s = activity.activity_today("2026-11-20")
    assert s["by_project"] == {"rose-MCP": 10, "(no project)": 6, "shop": 3}
    assert s["by_app"] == {"Code": 13, "Brave": 6} and s["active_minutes"] == 19
    assert s["first_activity"] == "09:00" and s["last_activity"] == "13:03"
    assert "No activity" in activity.activity_today("2026-11-19")["note"]
    with pytest.raises(Exception, match="YYYY-MM-DD"):
        activity.activity_today("nope")


def test_standup(act, monkeypatch):
    _, tmp = act
    today = date.today()
    y = today - timedelta(days=1)
    if y.weekday() == 6:
        y -= timedelta(days=2)
    _write(tmp, f"{y:%Y-%m-%d}", [{"ts": f"{y}T10:00:00", "app": "Code", "project": "rose-MCP", "title": "a"},
                                  {"ts": f"{y}T10:05:00", "idle": True}])
    (tmp / "claude_events.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"ts": f"{y}T10:01:00", "event": "UserPromptSubmit", "session_id": "s1", "project": "rose-MCP"},
        {"ts": datetime.combine(y, datetime.min.time()).timestamp() + 40000, "event": "Stop", "session_id": "s2",
         "cwd": "/x/shop"},
    ]) + "\nnot json\n")
    monkeypatch.setattr(activity, "_git_email", lambda: "me@x")
    monkeypatch.setattr(activity, "commits", lambda d, e: {"rose-MCP": ["fix a", "add b"]} if d == y else {})
    text = activity.standup()
    assert "Yesterday" in text and "rose-MCP: 2 commits: fix a; add b" in text
    assert "Claude Code: 2 sessions (rose-MCP, shop)" in text and "Time: 5m active" in text
    assert "Blockers:" in text


def test_commits_real_repo(act, tmp_path, monkeypatch):
    import subprocess
    repo = tmp_path / "r"
    repo.mkdir()
    env = {"GIT_AUTHOR_EMAIL": "me@x", "GIT_COMMITTER_EMAIL": "me@x", "GIT_AUTHOR_NAME": "me",
           "GIT_COMMITTER_NAME": "me", "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "hello"], check=True, env=env)
    monkeypatch.setattr(activity, "_cache", {"proj_at": 9e12, "proj": [{"name": "r", "path": str(repo)}]})
    assert activity.commits(date.today(), "me@x") == {"r": ["hello"]}
    assert activity.commits(date.today(), "other@x") == {}


def test_daily_summary_push_once(act, monkeypatch):
    cfg = {}
    monkeypatch.setattr(activity, "load_config", lambda: cfg)

    def settings(**kw):
        cfg.update(kw)

    _, tmp = act
    pushes = []
    import rose.push
    monkeypatch.setattr(rose.push, "push", lambda t, m="", **k: pushes.append((t, m)))
    monkeypatch.setattr(activity, "sample", lambda: None)
    monkeypatch.setattr(activity, "commits", lambda d, e: {})
    settings(activity_summary_time="22:30")
    now = datetime(2026, 11, 20, 22, 0)
    _write(tmp, "2026-11-20", [{"ts": "2026-11-20T09:00:00", "app": "Code", "project": "shop", "title": "a"},
                               {"ts": "2026-11-20T09:05:00", "idle": True}])
    state = {}
    activity.tick(state, now)
    assert pushes == []
    activity.tick(state, now.replace(minute=31))
    activity.tick(state, now.replace(minute=45))
    assert len(pushes) == 1 and "shop 5m" in pushes[0][1]
    settings(activity_enabled=False)
    activity.tick({}, now.replace(minute=31))
    assert len(pushes) == 1


def test_prune_old_logs(act, monkeypatch):
    _, tmp = act
    monkeypatch.setattr(activity, "sample", lambda: None)
    _write(tmp, "2020-01-01", [])
    _write(tmp, f"{date.today():%Y-%m-%d}", [])
    activity.tick({})
    assert [p.stem for p in (tmp / "activity").iterdir()] == [f"{date.today():%Y-%m-%d}"]
