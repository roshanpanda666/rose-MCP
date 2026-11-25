"""Files+ (copy, trash, edit, zip), new_project, git_branch, sort_downloads, clipboard history, docker guard,
all with their undo paths."""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from rose.common import ToolError
from rose.tools import clipboard, dev, docker, files, organize, projects, undo


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(undo, "UNDO_DIR", tmp_path / "undo")
    monkeypatch.setattr(undo, "JOURNAL", tmp_path / "undo" / "journal.jsonl")
    (tmp_path / "ws").mkdir()
    monkeypatch.setattr(files, "allowed_dirs", lambda: [(tmp_path / "ws").resolve()])
    return (tmp_path / "ws").resolve()


def test_copy_file_and_undo(ws):
    (ws / "a.txt").write_text("A")
    (ws / "d").mkdir()
    (ws / "d" / "x.txt").write_text("X")
    undo.undoable(files.copy_file)(str(ws / "a.txt"), str(ws / "d"))
    assert (ws / "d" / "a.txt").read_text() == "A"
    undo.undoable(files.copy_file)(str(ws / "d"), str(ws / "d2"))
    assert (ws / "d2" / "x.txt").read_text() == "X"
    with pytest.raises(ToolError, match="confirm=true"):
        files.copy_file(str(ws / "a.txt"), str(ws / "d" / "a.txt"))
    with pytest.raises(ToolError, match="into itself"):
        files.copy_file(str(ws / "d"), str(ws / "d" / "inner"))
    undo.undo_last(confirm=True)
    assert not (ws / "d2").exists()
    undo.undo_last(confirm=True)
    assert not (ws / "d" / "a.txt").exists() and (ws / "a.txt").exists()


def test_edit_file_and_undo(ws):
    f = ws / "n.md"
    f.write_text("one two two\n")
    with pytest.raises(ToolError, match="2 times"):
        files.edit_file(str(f), find="two", replace="2")
    with pytest.raises(ToolError, match="not found"):
        files.edit_file(str(f), find="three", replace="3")
    undo.undoable(files.edit_file)(str(f), find="one", replace="1", append="end")
    assert f.read_text() == "1 two two\nend"
    files.edit_file(str(f), find="two", replace="2", replace_all=True)
    assert f.read_text() == "1 2 2\nend"
    undo.undo_last(confirm=True)
    assert f.read_text() == "one two two\n"


def test_trash_and_restore(ws, tmp_path, monkeypatch):
    monkeypatch.setattr(undo.Path, "home", classmethod(lambda cls: tmp_path))
    trash = tmp_path / ".local" / "share" / "Trash"
    (trash / "files").mkdir(parents=True)
    (trash / "info").mkdir()
    monkeypatch.setattr(files, "TRASH", trash)
    f = ws / "old.txt"
    f.write_text("bye")

    def fake_gio(cmd, **kw):
        p = os.fspath(cmd[-1])
        name = os.path.basename(p)
        os.rename(p, trash / "files" / name)
        (trash / "info" / f"{name}.trashinfo").write_text(f"[Trash Info]\nPath={p}\nDeletionDate=2026-11-25T21:00:00\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(files.subprocess, "run", fake_gio)
    with pytest.raises(ToolError, match="confirm=true"):
        files.trash_file(str(f))
    with pytest.raises(ToolError, match="root folder"):
        files.trash_file(str(ws), confirm=True)
    assert "(as old.txt)" in undo.undoable(files.trash_file)(str(f), confirm=True)
    assert not f.exists()
    assert "restored old.txt" in undo.undo_last()
    assert f.read_text() == "bye" and not (trash / "info" / "old.txt.trashinfo").exists()


def test_zip_unzip_and_undo(ws):
    (ws / "p").mkdir()
    (ws / "p" / "a.txt").write_text("a")
    (ws / "b.txt").write_text("b")
    assert "2 files" in undo.undoable(files.zip_files)([str(ws / "p"), str(ws / "b.txt")], str(ws / "out"))
    assert (ws / "out.zip").exists()
    with pytest.raises(ToolError, match="already exists"):
        files.zip_files([str(ws / "b.txt")], str(ws / "out.zip"))
    undo.undoable(files.unzip_file)(str(ws / "out.zip"))
    assert (ws / "out" / "p" / "a.txt").read_text() == "a" and (ws / "out" / "b.txt").exists()
    with pytest.raises(ToolError, match="already exists"):
        files.unzip_file(str(ws / "out.zip"))
    undo.undo_last(confirm=True)
    assert not (ws / "out").exists()


def test_unzip_refuses_zip_slip(ws):
    import zipfile

    with zipfile.ZipFile(ws / "evil.zip", "w") as z:
        z.writestr("../escape.txt", "x")
    with pytest.raises(ToolError, match="unsafe path"):
        files.unzip_file(str(ws / "evil.zip"))


def test_new_project_and_branches(tmp_path, monkeypatch):
    monkeypatch.setattr(undo, "UNDO_DIR", tmp_path / "undo")
    monkeypatch.setattr(undo, "JOURNAL", tmp_path / "undo" / "journal.jsonl")
    root = tmp_path / "0x"
    (root / "projects").mkdir(parents=True)
    monkeypatch.setattr(projects, "_root", lambda: root)
    r = undo.undoable(dev.new_project)("My Tool", kind="python", open_in_code=False)
    path = root / "projects" / "My-Tool"
    assert r["path"] == str(path) and (path / ".git").is_dir() and "__pycache__" in (path / ".gitignore").read_text()
    with pytest.raises(ToolError, match="already exists"):
        dev.new_project("My Tool", open_in_code=False)
    with pytest.raises(ToolError, match="confirm=true"):
        undo.undo_last()
    undo.undo_last(confirm=True)
    assert not path.exists()

    dev.new_project("repo", open_in_code=False)
    repo = root / "projects" / "repo"
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True, env=env)
    assert dev.git_branch(str(repo))["current"] == "main"
    r = undo.undoable(dev.git_branch)(str(repo), "create", "feature/x")
    assert r["branch"] == "feature/x" and r["previous"] == "main"
    with pytest.raises(ToolError, match="already exists"):
        dev.git_branch(str(repo), "create", "feature/x")
    with pytest.raises(ToolError, match="valid branch"):
        dev.git_branch(str(repo), "create", "bad..name")
    (repo / "README.md").write_text("changed\n")
    with pytest.raises(ToolError, match="uncommitted"):
        dev.git_branch(str(repo), "switch", "main")
    subprocess.run(["git", "-C", str(repo), "checkout", "--", "README.md"], check=True)
    assert "back on main and deleted feature/x" in undo.undo_last()
    assert [b["branch"] for b in dev.git_branch(str(repo))["branches"]] == ["main"]


def test_sort_downloads_and_undo(tmp_path, monkeypatch):
    monkeypatch.setattr(undo, "UNDO_DIR", tmp_path / "undo")
    monkeypatch.setattr(undo, "JOURNAL", tmp_path / "undo" / "journal.jsonl")
    dl = tmp_path / "Downloads"
    dl.mkdir()
    monkeypatch.setattr(organize, "DOWNLOADS", dl)
    old = time.time() - 3 * 86400
    for name in ("pic.png", "doc.pdf", "x.tar.gz", "weird.xyz", "half.crdownload", ".hidden"):
        (dl / name).write_text("x")
        os.utime(dl / name, (old, old))
    (dl / "fresh.png").write_text("x")
    (dl / "Images").mkdir()
    (dl / "Images" / "pic.png").write_text("existing")
    plan = organize.sort_downloads()
    assert plan["dry_run"] and plan["files"] == 4 and (dl / "pic.png").exists()
    r = undo.undoable(organize.sort_downloads)(dry_run=False)
    assert r["moved"] == 4
    assert (dl / "Images" / "pic (2).png").exists() and (dl / "Archives" / "x.tar.gz").exists()
    assert (dl / "Other" / "weird.xyz").exists() and (dl / "half.crdownload").exists() and (dl / "fresh.png").exists()
    assert "moved 4 file(s) back" in undo.undo_last()
    assert (dl / "pic.png").exists() and not (dl / "Documents").exists() and (dl / "Images" / "pic.png").exists()


def test_clipboard_history(tmp_path, monkeypatch):
    monkeypatch.setattr(clipboard, "HISTORY", tmp_path / "hist.json")
    monkeypatch.setattr(clipboard, "ROSE_HOME", tmp_path)
    copied = []
    monkeypatch.setattr(clipboard, "clipboard_write", lambda t: copied.append(t))
    for secret in ("sk-abc123def456", "hunter2pass9", "password: x", "ghp_aaaaaaaaaaaaaaaaaaaa"):
        assert not clipboard.record_clip(secret)
    assert clipboard.record_clip("first note")
    assert clipboard.record_clip("https://example.com/a1b2c3d4")
    assert clipboard.record_clip("second note")
    assert not clipboard.record_clip("second note")
    assert clipboard.record_clip("first note")  # moves to newest
    rows = clipboard.clipboard_history()
    assert [r["text"] for r in rows] == ["first note", "second note", "https://example.com/a1b2c3d4"]
    assert oct((tmp_path / "hist.json").stat().st_mode & 0o777) == "0o600"
    clipboard.clipboard_history("copy", n=2)
    assert copied == ["second note"]
    assert clipboard.clipboard_history(query="example")[0]["n"] == 1
    with pytest.raises(ToolError, match="No clip 9"):
        clipboard.clipboard_history("copy", n=9)
    clipboard.clipboard_history("clear")
    assert "none" in clipboard.clipboard_history()[0]


def test_docker_stop_needs_confirm(monkeypatch):
    monkeypatch.setattr(docker, "_containers", lambda all_: [{"name": "db", "image": "postgres", "state": "running"}])
    calls = []
    monkeypatch.setattr(docker, "_docker", lambda *a, **k: calls.append(a) or "")
    with pytest.raises(ToolError, match="confirm=true"):
        docker.docker("stop", "db")
    assert docker.docker("stop", "post", confirm=True) == "Stopped db."
    assert docker.docker("start", "db") == "Started db."
    assert calls == [("stop", "db"), ("start", "db")]
    with pytest.raises(ToolError, match="No container"):
        docker.docker("logs", "redis")


def test_uv_upgrade_never_touches_local_tools(tmp_path, monkeypatch):
    from rose.tools import packages

    (tmp_path / "rose").mkdir()
    (tmp_path / "rose" / "uv-receipt.toml").write_text('requirements = [{ name = "rose", directory = "/x" }]\n')
    (tmp_path / "ruff").mkdir()
    (tmp_path / "ruff" / "uv-receipt.toml").write_text('requirements = [{ name = "ruff" }]\n')
    monkeypatch.setattr(packages, "UV_TOOLS", tmp_path)
    monkeypatch.setattr(packages.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(packages, "_out", lambda cmd, timeout=60: "rose v0.3.0 [latest: 1.0.0]\n- rose\n"
                        "ruff v0.1 [latest: 0.2]\n- ruff\n")
    assert packages._uv()["names"] == ["ruff"]
    launched = []
    monkeypatch.setattr(packages, "launch_detached", lambda cmd, **k: launched.append(cmd))
    with pytest.raises(ToolError, match="confirm=true"):
        packages.package_updates("upgrade", "uv")
    r = packages.package_updates("upgrade", "uv", confirm=True)
    assert r["command"] == "uv tool upgrade ruff" and "rose" not in launched[0][-1].split("upgrade", 1)[1]
