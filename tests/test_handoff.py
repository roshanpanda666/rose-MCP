"""hand_to_claude: right window, prompt in the URI, Enter only once the new Claude tab is in front."""

from __future__ import annotations

from pathlib import Path

import pytest

from rose.common import ToolError
from rose.tools import handoff


@pytest.fixture
def vs(monkeypatch):
    titles = ["main.py - rose-MCP - Visual Studio Code"]
    sent, keys = [], []
    win = {"id": "0x1", "title": titles[0]}
    monkeypatch.setattr(handoff, "_vscode_windows", lambda: [dict(win, title=titles[-1])])
    monkeypatch.setattr(handoff, "_active_title", lambda: titles[-1])
    monkeypatch.setattr(handoff.shutil, "which", lambda n: "/usr/bin/code")
    monkeypatch.setattr(handoff.projects, "_index",
                        lambda: [{"name": "rose-MCP", "path": "/x/rose-MCP", "group": "personal", "depth": 2}])
    monkeypatch.setattr("rose.tools.apps.focus_window", lambda w: "ok")

    def launch(cmd, cwd=None, name=""):
        sent.append(cmd)
        if cmd[1] == "--open-url":
            titles.append("Claude Code - rose-MCP - Visual Studio Code")
    monkeypatch.setattr(handoff, "launch_detached", launch)
    monkeypatch.setattr(handoff, "run", lambda cmd: keys.append(cmd))
    monkeypatch.setattr(handoff, "_focus_claude_input", lambda t: keys.append("focus-input") or True)
    monkeypatch.setattr(handoff, "_active_id", lambda: "0x1")
    monkeypatch.setattr(handoff.time, "sleep", lambda s: None)
    return titles, sent, keys


def test_needs_confirm(vs):
    with pytest.raises(ToolError, match="confirm=true"):
        handoff.hand_to_claude("fix the tests")


def test_sends_to_front_window(vs):
    titles, sent, keys = vs
    msg = handoff.hand_to_claude("Fix the failing test in tests/test_x.py & explain", confirm=True)
    assert sent[-1][:2] == ["/usr/bin/code", "--open-url"]
    assert sent[-1][2] == handoff.URI + "Fix%20the%20failing%20test%20in%20tests%2Ftest_x.py%20%26%20explain"
    assert keys == ["focus-input", ["xdotool", "key", "--clearmodifiers", "Return"]]
    assert "Sent to Claude Code" in msg and "rose-MCP" in msg


def test_no_enter_when_tab_not_confirmed(vs, monkeypatch):
    titles, sent, keys = vs
    monkeypatch.setattr(handoff, "launch_detached", lambda cmd, **kw: sent.append(cmd))  # title never changes
    monkeypatch.setattr(handoff, "_wait", lambda pred, s, step=0.15: pred() or None)
    msg = handoff.hand_to_claude("do it", confirm=True)
    assert keys == [] and "NOT press Enter" in msg


def test_folder_from_title():
    assert handoff._folder_of("Claude Code - rose-MCP - Visual Studio Code") == "rose-MCP"
    assert handoff._folder_of("Visual Studio Code") == ""
    assert Path  # noqa


def test_session_dir_encoding():
    assert handoff.session_dir(Path("/home/roshan/0x/projects/rose-MCP")).name == "-home-roshan-0x-projects-rose-MCP"
    assert handoff.session_dir(Path("/home/r/.config/a_b")).name == "-home-r--config-a-b"


def test_continue_session_adds_newest(vs, monkeypatch, tmp_path):
    titles, sent, keys = vs
    monkeypatch.setattr(handoff, "CLAUDE_PROJECTS", tmp_path)
    d = tmp_path / "-x-rose-MCP"
    d.mkdir()
    old, new = "11111111-2222-3333-4444-555555555555", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    import os
    (d / f"{old}.jsonl").write_text("{}")
    (d / f"{new}.jsonl").write_text("{}")
    (d / "notes.jsonl").write_text("{}")
    os.utime(d / f"{old}.jsonl", (1, 1))
    msg = handoff.hand_to_claude("go on", confirm=True, continue_session=True)
    assert sent[-1][2] == handoff.URI + "go%20on&session=" + new
    assert "Continued session aaaaaaaa" in msg and keys


def test_continue_session_falls_back(vs, monkeypatch, tmp_path):
    titles, sent, keys = vs
    monkeypatch.setattr(handoff, "CLAUDE_PROJECTS", tmp_path)
    msg = handoff.hand_to_claude("go on", confirm=True, continue_session=True)
    assert "session=" not in sent[-1][2] and "new session" in msg


def test_background_job(vs, monkeypatch, tmp_path):
    titles, sent, keys = vs
    proj = tmp_path / "rose-MCP"
    proj.mkdir()
    monkeypatch.setattr(handoff.projects, "_index",
                        lambda: [{"name": "rose-MCP", "path": str(proj), "group": "personal", "depth": 2}])
    monkeypatch.setattr(handoff, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(handoff.os.path, "exists", lambda p: True)
    launched = []
    monkeypatch.setattr(handoff, "launch_detached", lambda cmd, cwd=None, name="": launched.append((cmd, cwd)))
    msg = handoff.hand_to_claude("run the tests and fix failures", confirm=True, mode="background")
    job_id = msg.split("job ")[1].split()[0]
    cmd, cwd = launched[0]
    assert cmd[-2:] == ["--job", str(tmp_path / "jobs" / job_id)] and cwd == str(proj)
    meta = handoff._read_meta(tmp_path / "jobs" / job_id)
    c = meta["cmd"]
    assert c[c.index("--permission-mode") + 1] == "acceptEdits"
    assert c[c.index("--tools") + 1] == "Read,Edit,Write,Glob,Grep,Bash"
    assert "--strict-mcp-config" in c and "Bash(git status:*)" in c and "Bash(git push:*)" in c
    assert "Bash" not in c[c.index("--allowedTools") + 1:c.index("--disallowedTools")]  # never bare Bash
    assert keys == [] and sent == []  # no VS Code, no keys
    st = handoff.claude_job_status(job_id)
    assert st["status"] in ("starting", "failed") and st["project"] == "rose-MCP"
    assert handoff.claude_job_status()["jobs"][0]["job_id"] == job_id


def test_job_status_bad_id():
    with pytest.raises(ToolError):
        handoff.claude_job_status("../x")


def test_enter_retried_until_submit_hook(monkeypatch, tmp_path):
    import json
    ev = tmp_path / "claude_events.jsonl"
    monkeypatch.setattr("rose.claude_events.EVENTS_PATH", ev)
    monkeypatch.setattr(handoff, "_active_title", lambda: "Claude Code - proj - Visual Studio Code")
    monkeypatch.setattr(handoff, "_focus_claude_input", lambda t: True)
    monkeypatch.setattr(handoff.time, "sleep", lambda s: None)
    monkeypatch.setattr(handoff, "_wait", lambda pred, s, step=0.15: pred() or None)
    keys = []

    def press(cmd):
        keys.append(cmd)
        if len(keys) == 3:  # third Enter lands
            ev.write_text(json.dumps({"ts": 100.0, "event": "UserPromptSubmit", "cwd": "/x/proj",
                                      "summary": "fix   the bug please", "message": ""}) + "\n")
    monkeypatch.setattr(handoff, "run", press)
    ev.write_text(json.dumps({"ts": 50.0, "event": "UserPromptSubmit", "cwd": "/x/proj",
                              "summary": "fix the bug please"}) + "\n")  # older: must not count
    assert handoff._press_enter_until_submitted(Path("/x/proj"), "fix the bug please", 99.0) is True
    assert len(keys) == 3


def test_enter_not_confirmed(monkeypatch, tmp_path):
    ev = tmp_path / "claude_events.jsonl"
    ev.write_text("")
    monkeypatch.setattr("rose.claude_events.EVENTS_PATH", ev)
    monkeypatch.setattr(handoff, "_active_title", lambda: "Claude Code - proj - Visual Studio Code")
    monkeypatch.setattr(handoff, "_focus_claude_input", lambda t: True)
    monkeypatch.setattr(handoff.time, "sleep", lambda s: None)
    monkeypatch.setattr(handoff, "_wait", lambda pred, s, step=0.15: pred() or None)
    keys = []
    monkeypatch.setattr(handoff, "run", keys.append)
    assert handoff._press_enter_until_submitted(Path("/x/proj"), "task", 0.0, tries=4) is False
    assert len(keys) == 4


def test_refocuses_window_and_input_before_enter(monkeypatch, tmp_path):
    """Focus stolen by another window: bring VS Code back by id, focus the Claude input, only then Enter."""
    monkeypatch.setattr("rose.claude_events.EVENTS_PATH", tmp_path / "none.jsonl")  # no hooks
    state = {"id": "0x9", "title": "Firefox"}
    calls = []

    def focus(w):
        calls.append(("focus", w))
        state.update(id="0x1", title="Claude Code - proj - Visual Studio Code")
    monkeypatch.setattr("rose.tools.apps.focus_window", focus)
    monkeypatch.setattr(handoff, "_active_id", lambda: state["id"])
    monkeypatch.setattr(handoff, "_active_title", lambda: state["title"])
    monkeypatch.setattr(handoff, "_focus_claude_input", lambda t: calls.append("input") or True)
    monkeypatch.setattr(handoff, "run", lambda cmd: calls.append(cmd[-1]))
    monkeypatch.setattr(handoff.time, "sleep", lambda s: None)
    monkeypatch.setattr(handoff, "_wait", lambda pred, s, step=0.15: pred() or None)
    assert handoff._press_enter_until_submitted(Path("/x/proj"), "t", 0.0, win_id="0x1") is True
    assert calls[:3] == [("focus", "0x1"), "input", "Return"]


def test_no_enter_when_window_cannot_be_focused(monkeypatch):
    monkeypatch.setattr("rose.tools.apps.focus_window", lambda w: "asked")
    monkeypatch.setattr(handoff, "_active_id", lambda: "0x9")
    monkeypatch.setattr(handoff, "_active_title", lambda: "Firefox")
    keys = []
    monkeypatch.setattr(handoff, "_focus_claude_input", lambda t: keys.append("input") or True)
    monkeypatch.setattr(handoff, "run", keys.append)
    monkeypatch.setattr(handoff.time, "sleep", lambda s: None)
    monkeypatch.setattr(handoff, "_wait", lambda pred, s, step=0.15: pred() or None)
    assert handoff._press_enter_until_submitted(Path("/x/proj"), "t", 0.0, win_id="0x1") is None
    assert keys == []


def test_no_enter_when_prompt_box_not_found(monkeypatch, tmp_path):
    """The prompt box can't be found on screen: never press Enter blind (it could land in an open file)."""
    monkeypatch.setattr("rose.claude_events.EVENTS_PATH", tmp_path / "none.jsonl")
    monkeypatch.setattr(handoff, "_active_title", lambda: "Claude Code - proj - Visual Studio Code")
    monkeypatch.setattr(handoff, "_focus_claude_input", lambda t: False)
    keys = []
    monkeypatch.setattr(handoff, "run", keys.append)
    monkeypatch.setattr(handoff.time, "sleep", lambda s: None)
    assert handoff._press_enter_until_submitted(Path("/x/proj"), "t", 0.0, tries=3) is False
    assert keys == []


def test_click_lands_on_lowest_best_match(monkeypatch):
    """OCR finds the task text in the editor and in the prompt box: click the box (lowest of the best matches)."""
    cands = [{"x": 500, "y": 300, "score": 0.97, "text": "fix the bug"},
             {"x": 1500, "y": 700, "score": 0.97, "text": "fix the bug"},
             {"x": 400, "y": 900, "score": 0.70, "text": "fix a bag"}]
    monkeypatch.setattr("rose.tools.pointer._ocr_candidates", lambda w, area: (cands, "active window"))
    clicks = []
    monkeypatch.setattr(handoff, "run", clicks.append)
    monkeypatch.setattr(handoff.time, "sleep", lambda s: None)
    assert handoff._focus_claude_input("fix the bug please now") is True
    assert clicks == [["xdotool", "mousemove", "1500", "700", "click", "1"]]
