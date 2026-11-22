"""Test setup: every test runs against a throwaway ROSE_HOME / config, never ~/rose or the live server.

The environment variables must be set before any `rose` module is imported, because rose.config reads
them at import time.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_HOME = Path(tempfile.mkdtemp(prefix="rose-test-"))
os.environ["ROSE_HOME"] = str(_HOME / "home")
os.environ["ROSE_CONFIG"] = str(_HOME / "config.toml")
os.environ.pop("FREELLMAPI_KEY", None)

import pytest  # noqa: E402

from rose import config  # noqa: E402


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """No desktop notifications or phone pushes from tests."""
    import rose.common
    import rose.guard
    import rose.push

    monkeypatch.setattr(rose.common, "notify_desktop", lambda *a, **k: None)
    monkeypatch.setattr(rose.guard, "notify_desktop", lambda *a, **k: None)
    monkeypatch.setattr(rose.push, "notify_desktop", lambda *a, **k: None)
    monkeypatch.setattr(rose.push, "push", lambda *a, **k: False)
    import rose.tools.forge
    import rose.tools.motion
    import rose.tools.power
    import rose.tools.recorder
    import rose.tools.webcam

    for mod in (rose.tools.power, rose.tools.recorder, rose.tools.webcam, rose.tools.forge, rose.tools.motion):
        monkeypatch.setattr(mod, "notify_desktop", lambda *a, **k: None)
    yield


@pytest.fixture
def workspace() -> Path:
    """The first allowed folder (relative file-tool paths land here)."""
    ws = config.allowed_dirs()[0]
    ws.mkdir(parents=True, exist_ok=True)
    return ws


@pytest.fixture
def settings():
    """Change config.toml for one test; the original is restored afterwards."""
    config.load_config()  # make sure the file exists
    original = config.CONFIG_PATH.read_text()

    def change(**values):
        config.update_settings(values)

    yield change
    config.CONFIG_PATH.write_text(original)


@pytest.fixture
def auth_state():
    """Fresh auth state with a known passphrase and one static token; returns (passphrase, token)."""
    import secrets
    import time

    from rose import auth

    token = "rose_" + secrets.token_urlsafe(16)
    state = auth._empty_state()
    state["passphrase"] = auth._hash_passphrase("correct horse battery staple")
    state["static_tokens"][auth._h(token)] = {"name": "tests", "created": int(time.time())}
    auth.save_state(state)
    yield "correct horse battery staple", token
    auth.STATE_PATH.unlink(missing_ok=True)
