"""click_on_screen: matching, ambiguity handling, and that it never clicks when unsure."""

from __future__ import annotations

import pytest

from rose.common import ToolError
from rose.tools import pointer


def test_score_prefers_exact_then_contained_phrase():
    t = pointer._norm("Sign in")
    assert pointer._score(t, pointer._norm("Sign in")) == 1.0
    assert pointer._score(t, pointer._norm("| Sign in")) == 1.0  # symbols are not words
    assert 0.85 <= pointer._score(t, pointer._norm("Sign in now")) < 1.0
    assert pointer._score(t, pointer._norm("Sign out")) < 0.8


@pytest.fixture
def clicks(monkeypatch):
    from rose.tools import input as inp

    done = []
    monkeypatch.setattr(inp, "_check", lambda: None)
    monkeypatch.setattr(inp, "_move", lambda x, y: done.append(("move", x, y)))
    monkeypatch.setattr(inp, "_active", lambda: {"title": "Mail"})
    monkeypatch.setattr(inp.time, "sleep", lambda s: None)
    monkeypatch.setattr(pointer, "run", lambda cmd, **k: done.append(tuple(cmd)) or b"")
    return done


def test_one_match_is_clicked(clicks, monkeypatch):
    monkeypatch.setattr(pointer, "_find", lambda t, a: ([{"text": "Send", "source": "tree:push button", "x": 50, "y": 60,
                                                          "score": 1.0}], "tree"))
    msg = pointer.click_on_screen("Send")
    assert ("move", 50, 60) in clicks and "Left-clicked 'Send' at screen (50, 60)" in msg


def test_several_matches_are_listed_not_clicked(clicks, monkeypatch):
    two = [{"text": "Send", "source": "ocr", "x": 50, "y": 60, "score": 1.0},
           {"text": "Send", "source": "ocr", "x": 900, "y": 60, "score": 1.0}]
    monkeypatch.setattr(pointer, "_find", lambda t, a: (two, "OCR"))
    with pytest.raises(ToolError, match="pick=N"):
        pointer.click_on_screen("Send")
    assert clicks == []
    pointer.click_on_screen("Send", pick=2)
    assert ("move", 900, 60) in clicks


def test_nothing_found_clicks_nothing(clicks, monkeypatch):
    monkeypatch.setattr(pointer, "_find", lambda t, a: ([], "OCR"))
    with pytest.raises(ToolError, match="Nothing was clicked"):
        pointer.click_on_screen("Unicorn")
    assert clicks == []


def test_click_on_screen_needs_remote_approval_like_mouse_click():
    from rose import guard

    assert "click_on_screen" in guard.GUI_TOOLS


def test_hover_only_moves(clicks, monkeypatch):
    monkeypatch.setattr(pointer, "_find", lambda t, a: ([{"text": "Pause", "source": "tree:push button", "x": 2880,
                                                          "y": 1030, "score": 1.0}], "trees"))
    msg = pointer.click_on_screen("Pause", button="none")
    assert clicks == [("move", 2880, 1030)] and "Not clicked" in msg


def test_mouse_move_tool(monkeypatch):
    from rose import guard
    from rose.tools import input as inp

    moved = []
    monkeypatch.setattr(inp, "_check", lambda: None)
    monkeypatch.setattr(inp, "_move", lambda x, y: moved.append((x, y)))
    assert "Not clicked" in inp.mouse_move(100, 200, coords="screen") and moved == [(100, 200)]
    assert "mouse_move" in guard.GUI_TOOLS
