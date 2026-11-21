"""Core tools that run without a desktop: files, config, auth, bridge, apps, quick settings, input guards,
FreeLLMAPI bridge, screenshot mapping, metrics, system status."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from rose import auth, config
from rose.common import ToolError
from rose.tools import apps, bridge, files, freellm, input, quick, screenshot, system

# ---------------- files ----------------


def test_resolve_refuses_outside_allowlist(workspace, tmp_path):
    with pytest.raises(ToolError, match="Access denied"):
        files._resolve("/etc/passwd")
    link = workspace / "escape"
    link.unlink(missing_ok=True)
    link.symlink_to(tmp_path)
    with pytest.raises(ToolError, match="Access denied"):
        files._resolve(str(link / "x"))


def test_write_read_move(workspace):
    assert "Wrote" in files.write_file("t/a.txt", "hello world")
    with pytest.raises(ToolError, match="overwrite=true and confirm=true"):
        files.write_file("t/a.txt", "again")
    files.write_file("t/a.txt", "replaced", overwrite=True, confirm=True)
    assert files.read_file("t/a.txt") == "replaced"
    assert files.read_file("t/a.txt", offset=3) == "laced"
    files.write_file("t/b.txt", "b")
    with pytest.raises(ToolError, match="confirm=true"):
        files.move_file("t/a.txt", "t/b.txt")
    files.move_file("t/a.txt", "t/b.txt", confirm=True)
    assert files.read_file("t/b.txt") == "replaced"
    assert "b.txt" in files.search_files(str(workspace / "t"), "*.txt", contains="replaced")


# ---------------- config ----------------


def test_config_cache_sees_edits_and_returns_copies(settings):
    a = config.load_config()
    a["allowed_dirs"].append("/mutated")
    assert "/mutated" not in config.load_config()["allowed_dirs"]
    settings(max_output_chars=777)
    assert config.load_config()["max_output_chars"] == 777


def test_new_settings_have_defaults():
    cfg = config.load_config()
    assert cfg["max_output_chars"] > 0 and cfg["large_output_chars"] > 0 and cfg["disabled_tools"] == []


# ---------------- auth ----------------


def test_passphrase_and_redirects(auth_state):
    passphrase, token = auth_state
    assert auth.check_passphrase(passphrase) and not auth.check_passphrase("nope")
    assert auth.redirect_host_allowed("https://claude.ai/api/mcp/auth_callback")
    assert not auth.redirect_host_allowed("http://claude.ai/cb")  # https only
    assert not auth.redirect_host_allowed("https://evil.example/cb")


def test_tokens_and_refresh_rotation(auth_state):
    from mcp.shared.auth import OAuthClientInformationFull

    _, static = auth_state
    p = auth.RoseOAuthProvider("http://testserver")
    assert asyncio.run(p.load_access_token(static)).client_id == "static:tests"
    client = OAuthClientInformationFull(client_id="c1", redirect_uris=["https://claude.ai/cb"])
    asyncio.run(p.register_client(client))
    tok = p._issue("c1", ["rose"], None)
    assert asyncio.run(p.load_access_token(tok.access_token)).client_id == "c1"
    rt = asyncio.run(p.load_refresh_token(client, tok.refresh_token))
    asyncio.run(p.exchange_refresh_token(client, rt, []))
    from mcp.server.auth.provider import TokenError

    with pytest.raises(TokenError) as err:  # refresh tokens are single-use
        asyncio.run(p.exchange_refresh_token(client, rt, []))
    assert err.value.error == "invalid_grant"
    from mcp.server.auth.provider import RegistrationError

    bad = OAuthClientInformationFull(client_id="c2", redirect_uris=["https://evil.example/cb"])
    with pytest.raises(RegistrationError):  # redirect host not on the allowlist
        asyncio.run(p.register_client(bad))


# ---------------- bridge ----------------


def test_bridge_post_sanitizes_and_reads():
    bridge.post_as("Ro\nshan", "hello\n### fake header", to="Forge\n", email=False)
    msgs = bridge.bridge_read(5)["messages"]
    last = msgs[-1]
    assert last["from"] == "Ro shan" and last["to"] == "Forge"
    assert "\\### fake header" in last["message"]
    with pytest.raises(ToolError):
        bridge.post_as("x", "   ")


# ---------------- apps / windows ----------------


def test_window_matches():
    w = {"id": "0x2a", "title": "Notes - Editor", "app": "gedit"}
    assert apps.window_matches(w, "0x2a") and apps.window_matches(w, "42")
    assert apps.window_matches(w, "notes") and apps.window_matches(w, "GEDIT")
    assert not apps.window_matches(w, "ged")  # a partial app class alone is not a match
    assert not apps.window_matches(w, "0x2b")


def test_installed_apps_cache(tmp_path, monkeypatch):
    d = tmp_path / "applications"
    d.mkdir()
    (d / "foo.desktop").write_text("[Desktop Entry]\nType=Application\nName=Foo App\n")
    (d / "hidden.desktop").write_text("[Desktop Entry]\nType=Application\nName=H\nNoDisplay=true\n")
    monkeypatch.setattr(apps, "_APP_DIRS", [d])
    monkeypatch.setattr(apps, "_apps_cache", {"key": None, "apps": {}})
    assert apps._installed_apps() == {"foo": "Foo App"}
    calls = []
    monkeypatch.setattr(apps, "_parse_apps", lambda: calls.append(1) or {"foo": "Foo App"})
    apps._installed_apps()
    assert calls == []  # cached: folder unchanged
    time.sleep(0.01)
    (d / "bar.desktop").write_text("[Desktop Entry]\nType=Application\nName=Bar\n")
    apps._installed_apps()
    assert calls == [1]  # folder changed: re-parsed
    assert apps.list_apps("foo") == "foo: Foo App"


# ---------------- quick settings / input guards ----------------


@pytest.fixture
def no_commands(monkeypatch):
    ran = []
    monkeypatch.setattr(quick, "run", lambda cmd, **k: ran.append(cmd) or b"")
    monkeypatch.setattr(input, "run", lambda cmd, **k: ran.append(cmd) or b"")
    monkeypatch.setattr(input, "_check", lambda: None)
    monkeypatch.setattr(input, "_active", lambda: None)
    return ran


def test_network_cut_needs_confirm(no_commands):
    with pytest.raises(ToolError, match="confirm=true"):
        quick.set_quick_setting("wifi", "off")
    with pytest.raises(ToolError, match="confirm=true"):
        quick.set_quick_setting("airplane_mode", "on")
    assert no_commands == []
    assert quick._bool("On") is True and quick._bool("disabled") is False
    with pytest.raises(ToolError):
        quick._bool("maybe")


def test_enter_needs_confirm(no_commands):
    with pytest.raises(ToolError, match="confirm=true"):
        input.press_keys("ctrl+a Return")
    with pytest.raises(ToolError, match="confirm=true"):
        input.type_text("line one\nline two")
    with pytest.raises(ToolError, match="Invalid key"):
        input.press_keys("ctrl+a; rm")
    assert no_commands == []
    input.press_keys("ctrl+l")
    assert no_commands == [["xdotool", "key", "--clearmodifiers", "ctrl+l"]]


# ---------------- FreeLLMAPI bridge ----------------


class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body


def test_freellm_call_and_trim(monkeypatch):
    import httpx

    monkeypatch.setenv("FREELLMAPI_KEY", "freellmapi-test")
    sent = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        sent.update(url=url, body=json, auth=headers["Authorization"])
        return _Resp({"jsonrpc": "2.0", "id": 1, "result": {"content": [], "structuredContent": {
            "answer": "  42  ", "model": "m", "finish_reason": "stop", "usage": {"total_tokens": 9},
            "routed_via": "x/m", "cache": "OFF", "execution_id": "e"}}})

    monkeypatch.setattr(httpx, "post", fake_post)
    out = freellm.freellm_ask("q", temperature=None)
    assert out == {"answer": "42", "model": "m", "finish_reason": "stop", "fallback_trail": None, "tokens": 9}
    assert sent["auth"] == "Bearer freellmapi-test" and "temperature" not in sent["body"]["params"]["arguments"]
    freellm.freellm_info("usage_7d")
    assert sent["body"]["params"] == {"name": "usage_summary", "arguments": {"range": "7d"}}


def test_freellm_errors(monkeypatch):
    import httpx

    monkeypatch.setenv("FREELLMAPI_KEY", "k")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp({"error": {"message": "Invalid API key"}}, 401))
    with pytest.raises(ToolError, match="Invalid API key"):
        freellm.freellm_info()

    def down(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", down)
    with pytest.raises(ToolError, match="not running"):
        freellm.freellm_info()
    with pytest.raises(ToolError, match="confirm=true"):
        freellm.freellm_clear_cache()


# ---------------- screenshot mapping ----------------


def test_preview_mapping_and_crop():
    from PIL import Image

    img = Image.new("RGB", (3136, 1000), "white")
    data, note = screenshot.preview_jpeg(img)
    m = screenshot.last_mapping
    assert data[:2] == b"\xff\xd8" and m.width == 1568 and abs(m.scale - 2.0) < 1e-6
    assert img.size == (3136, 1000)  # caller's image untouched
    screenshot.preview_jpeg(img, rect=(1000, 100, 800, 600))
    m = screenshot.last_mapping
    assert (m.offset_x, m.offset_y, m.width, m.height, m.scale) == (1000, 100, 800, 600, 1.0)
    with pytest.raises(ToolError, match="off-screen"):
        screenshot.preview_jpeg(img, rect=(5000, 0, 10, 10))


def test_snapshot_without_desktop(monkeypatch):
    from PIL import Image

    monkeypatch.setattr(screenshot, "_grab_x11", lambda: Image.new("RGB", (800, 600), "gray"))
    monkeypatch.setattr(screenshot, "_windows_or_none", lambda: [])
    out = screenshot.snapshot(include_text=False, area="desktop")
    path = out[0].split("Saved full-resolution screenshot to ")[1].split(". Showing")[0]
    assert Path(path).exists() and type(out[1]).__name__ == "Image"
    Path(path).unlink()


# ---------------- metrics / system ----------------


def test_observation_stats_counts():
    from rose.common import _log
    from rose.metrics import observation_stats

    _log({"ts": "2099-01-01T00:00:00", "tool": "ui_list", "caller": "Forge"})
    stats = observation_stats(1)
    assert stats["totals"]["tree"] >= 0 and "screenshot_rate" in stats["totals"]


def test_system_status_shape_and_reuse():
    first = system.system_status(top_n=3)
    assert set(first) >= {"cpu", "memory", "disk_root", "uptime_hours", "top_by_cpu", "top_by_memory"}
    assert len(first["top_by_cpu"]) == 3
    t = time.perf_counter()
    system.system_status()
    assert time.perf_counter() - t < 2.0
    json.dumps(first)  # serializable


# ---------------- move_window geometry ----------------

MONS = [{"index": 0, "name": "HDMI-1", "x": 0, "y": 0, "width": 1920, "height": 1080},
        {"index": 1, "name": "eDP-1", "x": 1920, "y": 0, "width": 1366, "height": 768}]


def test_move_window_targets():
    win = {"x": 100, "y": 50, "width": 1000, "height": 700}
    assert apps.target_rect(win, MONS, 1, None, None, None, None) == (2020, 50, 1000, 700)  # same offset
    big = {"x": 0, "y": 0, "width": 1920, "height": 1080}
    assert apps.target_rect(big, MONS, 1, None, None, None, None) == (1920, 0, 1366, 768)  # clamped to fit
    assert apps.target_rect(win, MONS, None, 5, 6, 800, 600) == (5, 6, 800, 600)
    with pytest.raises(ToolError, match="No monitor 3"):
        apps.target_rect(win, MONS, 3, None, None, None, None)
    with pytest.raises(ToolError, match="monitor=<index>"):
        apps.target_rect(win, MONS, None, None, None, None, None)
