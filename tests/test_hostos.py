"""rose.hostos: OS detection, the host_os override, and per-OS system commands."""

from __future__ import annotations

import pytest

from rose import hostos
from rose.config import update_settings


@pytest.fixture(autouse=True)
def _reset_os():
    yield
    update_settings({"host_os": "auto"})


def test_auto_uses_detected_os():
    update_settings({"host_os": "auto"})
    assert hostos.setting() == "auto"
    assert hostos.current() == hostos.detected()


@pytest.mark.parametrize("name", hostos.SUPPORTED)
def test_override_picks_that_os(name):
    update_settings({"host_os": name})
    assert hostos.current() == name


def test_unknown_value_falls_back_to_auto():
    update_settings({"host_os": "beos"})
    assert hostos.setting() == "auto"


def test_commands_differ_per_os():
    assert hostos.command("open", "linux", target="x") == ["xdg-open", "x"]
    assert hostos.command("open", "macos", target="x") == ["open", "x"]
    assert hostos.command("play_wav", "macos", path="a.wav") == ["afplay", "a.wav"]
    assert hostos.command("play_wav", "linux", path="a.wav") == ["paplay", "a.wav"]


def test_missing_command_is_unsupported():
    with pytest.raises(hostos.Unsupported, match="Windows"):
        hostos.command("services", "windows")
    assert not hostos.supports("record_mic", "windows")


def test_info_lists_every_job_for_the_chosen_os():
    update_settings({"host_os": "windows"})
    info = hostos.info()
    assert info["current"] == "windows" and info["setting"] == "windows"
    assert {j["job"] for j in info["jobs"]} == set(hostos.COMMANDS)
    assert not next(j for j in info["jobs"] if j["job"] == "services")["supported"]
    assert [o["value"] for o in info["options"]] == ["auto", *hostos.SUPPORTED]


def test_process_running_finds_this_test():
    assert hostos.process_running("pytest")
    assert not hostos.process_running("no-such-process-zz9")
