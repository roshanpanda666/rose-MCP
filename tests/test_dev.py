"""Dev tools: git status/diff/commit/today's log on tmp repos, run presets, run logs, error context."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from rose.common import ToolError
from rose.tools import dev, projects


def _git(d: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(d), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "_root", lambda: tmp_path)
    monkeypatch.setattr(dev, "RUNS_DIR", tmp_path / "runs")
    return tmp_path


def _repo(root: Path, rel: str) -> Path:
    d = root / rel
    d.mkdir(parents=True)
    _git(d, "init", "-q", "-b", "main")
    _git(d, "config", "user.email", "me@example.com")
    _git(d, "config", "user.name", "Me")
    _git(d, "config", "commit.gpgsign", "false")
    (d / "a.py").write_text("x = 1\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "first")
    return d


def test_resolve_ambiguous_and_missing(root):
    (root / "projects/rms-a").mkdir(parents=True)
    (root / "projects/rms-b").mkdir(parents=True)
    with pytest.raises(ToolError, match="Several projects"):
        dev._resolve("rms")
    with pytest.raises(ToolError, match="No project"):
        dev._resolve("zzzzqqq")


def test_resolve_recent(root, monkeypatch):
    monkeypatch.setattr(projects, "_recent_folder", lambda: {"path": str(root)})
    assert dev._resolve("") == root and dev._resolve("last") == root


def test_status_and_diff(root):
    d = _repo(root, "projects/alpha")
    (d / "a.py").write_text("x = 2\n")
    (d / "new.txt").write_text("hi\n")
    (d / "b.py").write_text("y\n")
    _git(d, "add", "b.py")
    s = dev.git_status("alpha")
    assert s["branch"] == "main" and s["staged"] == 1 and s["unstaged"] == 1 and s["untracked"] == 1
    assert not s["clean"]
    diff = dev.git_diff_summary("alpha")
    assert "a.py" in diff["stat"] and "+x = 2" in diff["diff"] and diff["untracked"] == ["new.txt"]
    assert "b.py" in dev.git_diff_summary("alpha", staged=True)["stat"]


def test_not_a_repo_and_outside_root(root, tmp_path_factory):
    (root / "projects/plain").mkdir(parents=True)
    with pytest.raises(ToolError, match="not a git repository"):
        dev.git_status("plain")
    outside = tmp_path_factory.mktemp("elsewhere")
    with pytest.raises(ToolError, match="outside"):
        dev.git_status(str(outside))


def test_commit_guards(root):
    d = _repo(root, "projects/beta")
    with pytest.raises(ToolError, match="empty"):
        dev.git_commit("beta", "  ", confirm=True)
    (d / "a.py").write_text("x = 3\n")
    with pytest.raises(ToolError, match="confirm"):
        dev.git_commit("beta", "msg", add_all=True)
    assert _git(d, "diff", "--cached", "--name-only") == ""  # nothing staged without confirm
    with pytest.raises(ToolError, match="Nothing staged"):
        dev.git_commit("beta", "msg", confirm=True)
    r = dev.git_commit("beta", "second", add_all=True, confirm=True)
    assert r["pushed"] is False and _git(d, "log", "-1", "--format=%s").strip() == "second"
    with pytest.raises(ToolError, match="clean"):
        dev.git_commit("beta", "third", add_all=True, confirm=True)


def test_commit_runs_hooks(root):
    d = _repo(root, "projects/hooked")
    hook = d / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\necho blocked by hook >&2\nexit 1\n")
    hook.chmod(0o755)
    (d / "a.py").write_text("x = 9\n")
    with pytest.raises(ToolError, match="blocked by hook"):
        dev.git_commit("hooked", "nope", add_all=True, confirm=True)


def test_log_today(root):
    a = _repo(root, "projects/one")
    _repo(root, "work/two")
    (root / "projects/notgit").mkdir()
    _git(a, "-c", "user.email=other@x.com", "-c", "user.name=Other", "commit", "-q", "--allow-empty", "-m", "theirs")
    r = dev.git_log_today()
    assert r["repos_checked"] == 2 and r["total"] == 2
    assert [c["message"] for c in r["commits"]["one"]] == ["first"]
    assert len(dev.git_log_today("two")["commits"]["two"]) == 1


def test_project_tasks(root):
    d = root / "projects/web"
    d.mkdir(parents=True)
    (d / "package.json").write_text(json.dumps({"scripts": {"dev": "vite", "test": "vitest"}}))
    (d / "pnpm-lock.yaml").write_text("")
    (d / "Makefile").write_text(".PHONY: build\nbuild:\n\techo hi\nVAR := 1\ntest: build\n\techo t\n%.o: %.c\n")
    (d / "manage.py").write_text("")
    (d / "compose.yaml").write_text("services: {}\n")
    (d / "pyproject.toml").write_text('[project]\nname="w"\n[project.scripts]\nwcli = "w:main"\n')
    (d / "tests").mkdir()
    t = dev.project_tasks("web")["tasks"]
    assert t["dev"] == "pnpm dev" and t["test"] == "pnpm test" and t["build"] == "make build"
    assert t["make:test"] == "make test" and t["compose-up"] == "docker compose up"
    assert "runserver" in t and t["wcli"].endswith("wcli") and "py:test" in t
    assert not any(k.startswith("%") or k == "VAR" for k in t)


def test_project_run_logs_and_explain(root, monkeypatch):
    d = root / "projects/runner"
    d.mkdir(parents=True)
    (d / "package.json").write_text(json.dumps({"scripts": {"boom": "x", "deploy": "x"}}))
    (d / "Makefile").write_text('boom2:\n\t@echo starting; echo \'File "src/app.py", line 12\'; exit 3\n')
    (d / "src").mkdir()
    (d / "src/app.py").write_text("")
    launched = []
    monkeypatch.setattr(dev, "launch_detached", lambda cmd, cwd=None, name="": launched.append(cmd))
    monkeypatch.setattr(dev.shutil, "which", lambda n: "/usr/bin/ghostty" if n == "ghostty" else None)
    with pytest.raises(ToolError, match="No task"):
        dev.project_run("runner", "rm -rf /")
    with pytest.raises(ToolError, match="confirm"):
        dev.project_run("runner", "deploy")
    r = dev.project_run("runner", "boom2")
    cmd = launched[-1]
    assert cmd[0] == "/usr/bin/ghostty" and f"--working-directory={d}" in cmd and cmd[2] == "-e"
    assert dev.project_logs("runner")["status"].startswith("running")
    # Run the wrapper ourselves (no terminal): feed Enter for the final "press Enter" prompt.
    inner = cmd[3:]
    subprocess.run(inner, input=b"\n", capture_output=True, timeout=60)
    logs = dev.project_logs("runner", lines=5)
    assert logs["status"] == "failed (exit 2)" and "starting" in logs["tail"]
    assert Path(r["log"] + ".exit").read_text().strip() == "2"  # make exits 2 on a failed recipe
    e = dev.explain_error("runner")
    assert "exit 2" in e["source"] and f"{d / 'src/app.py'}:12" in e["file_refs"]


def test_explain_error_falls_back_to_screen(root, monkeypatch):
    monkeypatch.setattr(dev, "_screen_text",
                        lambda: "SCREEN AS TEXT\nerror TS2322 at src/main.ts:41:7 and lib/x.rs:3")
    e = dev.explain_error()
    assert e["source"].startswith("OCR") and e["file_refs"] == ["src/main.ts:41", "lib/x.rs:3"]


def test_refs_skip_site_packages():
    text = 'File "/usr/lib/python3/site-packages/x.py", line 5\nFile "/home/u/app.py", line 7'
    assert dev._refs(text) == ["/home/u/app.py:7"]
