"""Undo journal: moves, writes, settings are reversible; file-destroying undos need confirm."""

from __future__ import annotations

import pytest

from rose.common import ToolError
from rose.tools import files, undo


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(undo, "UNDO_DIR", tmp_path / "undo")
    monkeypatch.setattr(undo, "JOURNAL", tmp_path / "undo" / "journal.jsonl")
    (tmp_path / "ws").mkdir()
    monkeypatch.setattr(files, "allowed_dirs", lambda: [(tmp_path / "ws").resolve()])
    return tmp_path / "ws"


def test_move_is_undone(ws):
    (ws / "a.txt").write_text("hi")
    undo.undoable(files.move_file)(str(ws / "a.txt"), str(ws / "b.txt"))
    assert (ws / "b.txt").exists() and undo.undo_history()[0]["action"].startswith("moved a.txt")
    assert "moved b.txt back" in undo.undo_last()
    assert (ws / "a.txt").read_text() == "hi" and not (ws / "b.txt").exists()
    with pytest.raises(ToolError, match="Nothing to undo"):
        undo.undo_last()


def test_overwrite_restore_needs_confirm(ws):
    f = ws / "notes.txt"
    f.write_text("old")
    undo.undoable(files.write_file)(str(f), "new", overwrite=True, confirm=True)
    assert f.read_text() == "new"
    with pytest.raises(ToolError, match="confirm=true"):
        undo.undo_last()
    assert f.read_text() == "new"  # nothing happened without confirm
    undo.undo_last(confirm=True)
    assert f.read_text() == "old"


def test_new_file_is_removed_on_undo(ws):
    undo.undoable(files.write_file)(str(ws / "fresh.txt"), "x", confirm=True)
    undo.undo_last(confirm=True)
    assert not (ws / "fresh.txt").exists()


def test_create_folder_and_undo(ws):
    assert "Created folder" in undo.undoable(files.create_folder)(str(ws / "proj" / "sub"))
    assert (ws / "proj" / "sub").is_dir()
    assert "already exists" in files.create_folder(str(ws / "proj" / "sub"))
    (ws / "f.txt").write_text("x")
    with pytest.raises(ToolError, match="is a file"):
        files.create_folder(str(ws / "f.txt"))
    with pytest.raises(ToolError, match="Access denied"):
        files.create_folder("/etc/rose-nope")
    assert "removed folder proj" in undo.undo_last()
    assert not (ws / "proj").exists()


def test_setting_is_restored(ws, monkeypatch):
    from rose.tools import quick

    state = {"night_light": False}
    monkeypatch.setattr(quick, "quick_settings", lambda: dict(state))

    def fake_set(setting, value, confirm=False, undo_after=0):
        state[setting] = value == "on"
        return "ok"

    fake_set.__name__ = "set_quick_setting"
    monkeypatch.setattr(quick, "set_quick_setting", fake_set)
    undo.undoable(fake_set)("night_light", "on")
    assert state["night_light"] is True
    undo.undo_last()
    assert state["night_light"] is False
