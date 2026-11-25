"""Projects under ~/0x: found by spoken name, opened in VS Code by default; open_path sends ~/0x to VS Code."""

from __future__ import annotations

import pytest

from rose.common import ToolError
from rose.tools import device, projects


@pytest.fixture
def root(tmp_path, monkeypatch):
    for d in ["projects/rose-MCP/rose", "projects/rose-phone-app", "work/rms-backend", "work/rms-frontend",
              "work/pcnhub", "tutes/test", "projects/rose-MCP/node_modules/x"]:
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "projects/rose-MCP/pyproject.toml").write_text("")
    (tmp_path / "projects/rose-MCP/rose/server.py").write_text("")
    monkeypatch.setattr(projects, "_root", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(projects, "launch_detached", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(device, "launch_detached", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(projects.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(device.shutil, "which", lambda n: "/usr/bin/" + n)
    return tmp_path, calls


@pytest.mark.parametrize("spoken, folder", [
    ("the rose project", "projects/rose-MCP"), ("Rose MCP", "projects/rose-MCP"),
    ("rose phone app", "projects/rose-phone-app"), ("rms backend", "work/rms-backend"), ("pcn hub", "work/pcnhub"),
])
def test_spoken_names(root, spoken, folder):
    tmp, _ = root
    assert [p["path"] for p in projects._match(spoken)] == [str(tmp / folder)]


def test_list_groups_and_skips(root):
    names = {p["name"]: p["group"] for p in projects.list_projects()}
    assert names["rms-backend"] == "work" and names["rose-MCP"] == "personal"
    assert "node_modules" not in names and "work" not in names and "projects" not in names


def test_open_in_code_by_default_with_file(root):
    tmp, calls = root
    projects.open_project("rose", file="server.py")
    assert calls[-1] == ["/usr/bin/code", str(tmp / "projects/rose-MCP"), str(tmp / "projects/rose-MCP/rose/server.py")]
    projects.open_project("pcnhub", app="files")
    assert calls[-1] == ["xdg-open", str(tmp / "work/pcnhub")]


def test_ambiguous_and_missing(root):
    with pytest.raises(ToolError, match="Several projects"):
        projects.open_project("rms")
    with pytest.raises(ToolError, match="No project"):
        projects.open_project("zzzzqqq")


def test_open_path_under_0x_uses_code(root, monkeypatch):
    tmp, calls = root
    from rose.tools import files
    monkeypatch.setattr(files, "_resolve", lambda p: tmp / p)
    device.open_path("projects/rose-MCP/rose/server.py")
    assert calls[-1] == ["/usr/bin/code", str(tmp / "projects/rose-MCP"), str(tmp / "projects/rose-MCP/rose/server.py")]
