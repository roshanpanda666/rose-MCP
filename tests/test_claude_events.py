"""Claude Code hooks -> ~/rose/claude_events.jsonl, claude_state.json, phone push; settings installer; job runner."""

from __future__ import annotations

import io
import json
import sys

import pytest

from rose import claude_events as ce

ENV = {"CLAUDE_CODE_ENTRYPOINT": "claude-vscode"}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(ce, "ROSE_HOME", tmp_path)
    monkeypatch.setattr(ce, "EVENTS_PATH", tmp_path / "claude_events.jsonl")
    monkeypatch.setattr(ce, "STATE_PATH", tmp_path / "claude_state.json")
    monkeypatch.setattr(ce, "HOOK_STATE_PATH", tmp_path / "claude_hook_state.json")
    return tmp_path


def _transcript(tmp_path, text="All 12 tests pass now. I fixed the off-by-one in parser.py."):
    p = tmp_path / "t.jsonl"
    lines = [{"type": "user", "message": {"role": "user", "content": "fix it"}},
             {"type": "assistant", "message": {"content": [{"type": "text", "text": "Looking."}]}},
             {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
             {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}},
             {"type": "system", "subtype": "stop"}]
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return str(p)


def state(home):
    return json.loads((home / "claude_state.json").read_text())


def test_prompt_then_stop_pushes(home, tmp_path):
    base = {"session_id": "s1", "cwd": "/home/r/0x/projects/rose-MCP", "transcript_path": _transcript(tmp_path)}
    assert ce.handle("UserPromptSubmit", {**base, "prompt": "fix the tests"}, ENV, now=1000.0) is None
    assert state(home) == {"state": "busy", "project": "rose-MCP", "message": "fix the tests",
                           "session_id": "s1", "ts": 1000.0}
    p = ce.handle("Stop", base, ENV, now=1100.0)
    assert p["title"] == "✅ Claude finished in rose-MCP"
    assert p["message"].startswith("All 12 tests pass now.")
    st = state(home)
    assert st["state"] == "done" and st["ts"] == 1100.0 and st["message"].startswith("All 12")
    events = [json.loads(x) for x in (home / "claude_events.jsonl").read_text().splitlines()]
    assert [e["event"] for e in events] == ["UserPromptSubmit", "Stop"]
    assert set(events[0]) == {"ts", "event", "session_id", "cwd", "project", "summary", "message"}


def test_quick_stop_no_push(home, tmp_path):
    base = {"session_id": "s1", "cwd": "/p/app", "transcript_path": _transcript(tmp_path)}
    ce.handle("UserPromptSubmit", base, ENV, now=1000.0)
    assert ce.handle("Stop", base, ENV, now=1010.0) is None
    assert state(home)["state"] == "done"


def test_notification_high_and_rate_limited(home):
    n = {"session_id": "s1", "cwd": "/p/app", "message": "Claude needs your permission to use Bash",
         "notification_type": "permission_prompt"}
    p = ce.handle("Notification", n, ENV, now=1000.0)
    assert p["priority"] == "high" and p["title"].startswith("⏳ Claude needs you in app: Claude needs your")
    assert state(home)["state"] == "waiting"
    assert ce.handle("Notification", n, ENV, now=1020.0) is None  # within the per-session gap
    assert ce.handle("Notification", n, ENV, now=1100.0) is not None


def test_idle_notification_quiet(home):
    ce.handle("Stop", {"session_id": "s", "cwd": "/p/app", "last_assistant_message": "done"}, ENV, now=1000.0)
    p = ce.handle("Notification", {"session_id": "s", "cwd": "/p/app", "message": "Claude is waiting for your input",
                                   "notification_type": "idle_prompt"}, ENV, now=1200.0)
    assert p is None and state(home)["state"] == "done"


@pytest.mark.parametrize("env", [{"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, {"ROSE_CLAUDE_JOB": "abc"},
                                 {"CLAUDE_CODE_SKIP_PLUGIN_MCP_SERVERS": "1", "MCP_CONNECTION_NONBLOCKING": "false"}])
def test_headless_ignored(home, env):
    assert ce.handle("Stop", {"session_id": "s", "cwd": "/p"}, env, now=1.0) is None
    assert not (home / "claude_events.jsonl").exists() and not (home / "claude_state.json").exists()


def test_push_disabled(home, monkeypatch):
    monkeypatch.setattr(ce, "_cfg", lambda: {"push": False, "min_task": 20, "gap": 60})
    assert ce.handle("Notification", {"session_id": "s", "cwd": "/p", "message": "x"}, ENV, now=1.0) is None
    assert state(home)["state"] == "waiting"


def test_transcript_tail_only(tmp_path, monkeypatch):
    monkeypatch.setattr(ce, "TAIL_BYTES", 400)
    p = tmp_path / "big.jsonl"
    filler = json.dumps({"type": "user", "message": {"content": "x" * 300}})
    last = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "final words"}]}})
    p.write_text("\n".join([filler] * 50 + [last]) + "\n")
    assert ce.last_assistant_text(str(p)) == "final words"
    assert ce.last_assistant_text(str(tmp_path / "missing.jsonl")) == ""


def test_hook_main_never_fails(home, capsys, monkeypatch):
    monkeypatch.setattr(ce, "headless", lambda env=None: False)
    assert ce.hook_main("Stop", io.StringIO("not json")) == 0
    assert ce.hook_main("", io.StringIO(json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s",
                                                    "cwd": "/p/x", "prompt": "hi"}))) == 0
    assert capsys.readouterr().out == ""
    assert state(home)["state"] == "busy"


def test_installer_merges_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(ce, "hook_command", lambda ev: f"/opt/py -m rose.claude_events {ev}")
    s = tmp_path / "settings.json"
    other = {"type": "command", "command": "notify-send hi"}
    s.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [{"hooks": [other]}],
                                                       "PreToolUse": [{"matcher": "Bash", "hooks": [other]}]}}))
    assert "Installed" in ce.install(s)
    d = json.loads(s.read_text())
    assert d["model"] == "opus" and d["hooks"]["PreToolUse"] == [{"matcher": "Bash", "hooks": [other]}]
    assert d["hooks"]["Stop"][0] == {"hooks": [other]}
    assert d["hooks"]["Stop"][1]["hooks"][0]["command"] == "/opt/py -m rose.claude_events Stop"
    assert set(d["hooks"]) == {"Stop", "PreToolUse", "UserPromptSubmit", "Notification"}
    assert list(tmp_path.glob("settings.json.rose-backup-*"))
    assert "Already installed" in ce.install(s)
    assert json.loads(s.read_text()) == d
    assert "installed:" in ce.status(s)
    assert "Removed" in ce.uninstall(s)
    assert json.loads(s.read_text()) == {"model": "opus", "hooks": {"Stop": [{"hooks": [other]}],
                                                                   "PreToolUse": [{"matcher": "Bash", "hooks": [other]}]}}
    assert "No ROSE hooks" in ce.uninstall(s)


def test_installer_new_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ce, "hook_command", lambda ev: f"rose claude-hook {ev}")
    s = tmp_path / "sub" / "settings.json"
    ce.install(s)
    assert set(json.loads(s.read_text())["hooks"]) == set(ce.HOOK_EVENTS)


def test_run_job_records_result_and_pushes(tmp_path, monkeypatch):
    pushes = []
    import rose.push
    monkeypatch.setattr(rose.push, "push", lambda *a, **k: pushes.append((a, k)))
    d = tmp_path / "job"
    d.mkdir()
    out = {"result": "Fixed 2 tests.", "is_error": False, "total_cost_usd": 0.1, "session_id": "abc",
           "permission_denials": [{"tool_name": "Bash"}]}
    cmd = [sys.executable, "-c", f"import json; print(json.dumps({out!r}))"]
    (d / "meta.json").write_text(json.dumps({"id": "abcdefghij", "status": "starting", "task": "t", "cwd": str(tmp_path),
                                             "cmd": cmd, "timeout": 30, "started": 1.0}))
    assert ce.run_job(str(d)) == 0
    m = json.loads((d / "meta.json").read_text())
    assert m["status"] == "done" and m["result"] == "Fixed 2 tests." and m["denied_tools"] == ["Bash"]
    assert pushes and pushes[0][0][0].startswith("✅ Claude job done in")


def test_run_job_failure(tmp_path, monkeypatch):
    import rose.push
    monkeypatch.setattr(rose.push, "push", lambda *a, **k: None)
    d = tmp_path / "job"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"id": "abcdefghij", "status": "starting", "task": "t", "cwd": str(tmp_path),
                                             "cmd": [sys.executable, "-c", "import sys; sys.exit(3)"],
                                             "timeout": 30, "started": 1.0}))
    ce.run_job(str(d))
    m = json.loads((d / "meta.json").read_text())
    assert m["status"] == "failed" and m["exit_code"] == 3
