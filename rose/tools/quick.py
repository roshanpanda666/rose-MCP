"""GNOME quick settings: Wi-Fi, Bluetooth, Airplane mode, Power mode, Night Light, Dark Style,
Do Not Disturb, Caffeine, volume, brightness, lock screen.

Changes that cut the network (Wi-Fi off, Airplane mode on) would also cut ROSE's own tunnel, leaving
nobody able to undo them remotely. So they need confirm=true and come with an automatic undo timer
scheduled through systemd (it runs even while ROSE is unreachable). The change itself is delayed a few
seconds so the tool's answer reaches the caller before the connection drops.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from rose.common import ToolError, display_env, run

RFKILL = ["org.gnome.SettingsDaemon.Rfkill", "/org/gnome/SettingsDaemon/Rfkill", "org.gnome.SettingsDaemon.Rfkill"]
POWER = ["--dest", "org.gnome.SettingsDaemon.Power", "--object-path", "/org/gnome/SettingsDaemon/Power"]
CAFFEINE_SCHEMAS = Path.home() / ".local/share/gnome-shell/extensions/caffeine@patapon.info/schemas"
CAFFEINE = ["--schemadir", str(CAFFEINE_SCHEMAS), "org.gnome.shell.extensions.caffeine"]
CUT_DELAY = 3  # seconds before a network-cutting change, so the reply gets out first
DEFAULT_UNDO = 120
MAX_UNDO = 1800

Setting = Literal["wifi", "bluetooth", "airplane_mode", "power_mode", "night_light", "dark_style",
                  "do_not_disturb", "caffeine", "volume", "mute", "brightness", "auto_suspend",
                  "screen_timeout"]


def _out(cmd: list[str]) -> str:
    return run(cmd, timeout=10).decode(errors="replace").strip()


def _gset(schema: list[str] | str, key: str, value: str | None = None) -> str:
    opts, name = (schema[:-1], schema[-1]) if isinstance(schema, list) else ([], schema)
    if value is None:  # options like --schemadir must come before the verb
        return _out(["gsettings", *opts, "get", name, key])
    run(["gsettings", *opts, "set", name, key, value])
    return value


def _busbool(prop: str, value: bool | None = None) -> bool:
    if value is None:
        return _out(["busctl", "--user", "get-property", *RFKILL, prop]).endswith("true")
    run(["busctl", "--user", "set-property", *RFKILL, prop, "b", "true" if value else "false"])
    return value


def _brightness(value: int | None = None) -> int | None:
    if value is None:
        out = _out(["gdbus", "call", "--session", *POWER, "--method", "org.freedesktop.DBus.Properties.Get",
                    "org.gnome.SettingsDaemon.Power.Screen", "Brightness"])
        m = re.search(r"(-?\d+)", out)
        return int(m.group(1)) if m and int(m.group(1)) >= 0 else None
    run(["gdbus", "call", "--session", *POWER, "--method", "org.freedesktop.DBus.Properties.Set",
         "org.gnome.SettingsDaemon.Power.Screen", "Brightness", f"<int32 {value}>"])
    return value


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def quick_settings() -> dict:
    """Current state of the quick-settings menu (Wi-Fi, Bluetooth, Airplane mode, Power mode, ...)."""
    ssid = _safe(lambda: next((l.split(":", 1)[1] for l in _out(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"])
                               .splitlines() if l.startswith("yes:")), None))
    vol = _safe(lambda: _out(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"]), "")
    m = re.search(r"([\d.]+)", vol or "")
    bt_powered = _safe(lambda: "Powered: yes" in subprocess.run(["bluetoothctl", "show"], capture_output=True,
                                                                text=True, timeout=5, env=display_env()).stdout)
    return {
        "wifi": _safe(lambda: _out(["nmcli", "radio", "wifi"]) == "enabled"),
        "wifi_network": ssid,
        "bluetooth": bool(bt_powered) and not _safe(lambda: _busbool("BluetoothAirplaneMode"), True),
        "airplane_mode": _safe(lambda: _busbool("AirplaneMode")),
        "power_mode": _safe(lambda: _out(["powerprofilesctl", "get"])),
        "night_light": _safe(lambda: _gset("org.gnome.settings-daemon.plugins.color", "night-light-enabled") == "true"),
        "dark_style": _safe(lambda: _gset("org.gnome.desktop.interface", "color-scheme") == "'prefer-dark'"),
        "do_not_disturb": _safe(lambda: _gset("org.gnome.desktop.notifications", "show-banners") == "false"),
        "caffeine": _safe(lambda: _gset(CAFFEINE, "user-enabled") == "true") if CAFFEINE_SCHEMAS.exists() else None,
        "volume_percent": round(float(m.group(1)) * 100) if m else None,
        "muted": "MUTED" in (vol or ""),
        "brightness_percent": _safe(_brightness),
        "auto_suspend_on_ac": _safe(lambda: _gset("org.gnome.settings-daemon.plugins.power",
                                                  "sleep-inactive-ac-type") != "'nothing'"),
        "screen_timeout_minutes": _safe(lambda: int(_gset("org.gnome.desktop.session", "idle-delay").split()[-1]) // 60),
    }


def _bool(value: str) -> bool:
    v = str(value).strip().lower()
    if v in ("on", "true", "yes", "1", "enable", "enabled"):
        return True
    if v in ("off", "false", "no", "0", "disable", "disabled"):
        return False
    raise ToolError(f"Use on/off, not '{value}'.")


def _schedule(cmd: list[str], seconds: int, name: str) -> None:
    """Run `cmd` later via a transient systemd user timer (survives ROSE restarts and network loss)."""
    if not shutil.which("systemd-run"):
        raise ToolError("systemd-run is not available, so ROSE can't schedule the automatic undo.")
    run(["systemd-run", "--user", "--collect", f"--unit=rose-{name}-{seconds}-{abs(hash(tuple(cmd))) % 10**6}",
         f"--on-active={seconds}s", "--setenv=DBUS_SESSION_BUS_ADDRESS=" + display_env()["DBUS_SESSION_BUS_ADDRESS"],
         *cmd])


def _network_cut(name: str, cut: list[str], restore: list[str], confirm: bool, undo_after: int) -> str:
    if not confirm:
        raise ToolError(f"This cuts the internet, including ROSE's own connection: remote access stops until it "
                        f"is undone. Ask the user, then call again with confirm=true. It is undone automatically "
                        f"after undo_after seconds (default {DEFAULT_UNDO}, max {MAX_UNDO}).")
    undo_after = max(30, min(int(undo_after or DEFAULT_UNDO), MAX_UNDO))
    _schedule(restore, CUT_DELAY + undo_after, f"undo-{name}")  # schedule the undo first
    _schedule(cut, CUT_DELAY, name)
    return (f"{name.replace('_', ' ').capitalize()} will switch in {CUT_DELAY}s. ROSE (and remote access) will be "
            f"unreachable until it is automatically undone in about {undo_after}s.")


def set_quick_setting(setting: Setting, value: str, confirm: bool = False, undo_after: int = DEFAULT_UNDO) -> str:
    """Change one quick setting. value: on/off for toggles; power-saver/balanced/performance for
    power_mode; 0-100 for volume and brightness; minutes (0 = never) for screen_timeout;
    auto_suspend on/off = whether the laptop sleeps when idle on AC power (asleep = ROSE unreachable). Wi-Fi off and Airplane mode on cut ROSE's own
    connection: they need confirm=true and are undone automatically after undo_after seconds."""
    if setting == "wifi":
        if _bool(value):
            run(["nmcli", "radio", "wifi", "on"])
            return "Wi-Fi on."
        return _network_cut("wifi_off", ["nmcli", "radio", "wifi", "off"], ["nmcli", "radio", "wifi", "on"],
                            confirm, undo_after)
    if setting == "airplane_mode":
        if not _bool(value):
            _busbool("AirplaneMode", False)
            return "Airplane mode off."
        busset = ["busctl", "--user", "set-property", *RFKILL, "AirplaneMode", "b"]
        return _network_cut("airplane_on", busset + ["true"], busset + ["false"], confirm, undo_after)
    if setting == "bluetooth":
        on = _bool(value)
        _busbool("BluetoothAirplaneMode", not on)  # what GNOME's own toggle does (rfkill)
        if on:
            subprocess.run(["bluetoothctl", "power", "on"], capture_output=True, timeout=10, env=display_env())
        return f"Bluetooth {'on' if on else 'off'}."
    if setting == "power_mode":
        mode = str(value).strip().lower().replace(" ", "-").replace("_", "-")
        mode = {"power-save": "power-saver", "saver": "power-saver", "battery": "power-saver"}.get(mode, mode)
        if mode not in ("power-saver", "balanced", "performance"):
            raise ToolError("power_mode must be power-saver, balanced or performance.")
        run(["powerprofilesctl", "set", mode])
        return f"Power mode: {mode}."
    if setting == "night_light":
        on = _bool(value)
        _gset("org.gnome.settings-daemon.plugins.color", "night-light-enabled", "true" if on else "false")
        return f"Night Light {'on' if on else 'off'}."
    if setting == "dark_style":
        on = _bool(value)
        _gset("org.gnome.desktop.interface", "color-scheme", "'prefer-dark'" if on else "'default'")
        return f"Dark Style {'on' if on else 'off'}."
    if setting == "do_not_disturb":
        on = _bool(value)
        _gset("org.gnome.desktop.notifications", "show-banners", "false" if on else "true")
        return f"Do Not Disturb {'on' if on else 'off'}."
    if setting == "caffeine":
        if not CAFFEINE_SCHEMAS.exists():
            raise ToolError("The Caffeine GNOME extension isn't installed.")
        on = _bool(value)
        if (_gset(CAFFEINE, "user-enabled") == "true") != on:
            flip = "false" if _gset(CAFFEINE, "cli-toggle") == "true" else "true"
            _gset(CAFFEINE, "cli-toggle", flip)  # the extension watches this key to toggle itself
        return f"Caffeine {'on (screen stays awake)' if on else 'off'}."
    if setting == "auto_suspend":
        on = _bool(value)  # on AC power; while asleep ROSE (and remote access) is unreachable
        _gset("org.gnome.settings-daemon.plugins.power", "sleep-inactive-ac-type", "'suspend'" if on else "'nothing'")
        return f"Auto-suspend on AC power {'on' if on else 'off (laptop stays awake when plugged in)'}."
    if setting == "screen_timeout":
        try:
            minutes = int(float(str(value).strip().lower().replace("never", "0").rstrip("m")))
        except ValueError:
            raise ToolError("screen_timeout needs minutes (0 = never).")
        minutes = max(0, min(minutes, 120))
        _gset("org.gnome.desktop.session", "idle-delay", f"uint32 {minutes * 60}")
        return f"Screen turns off after {minutes} min." if minutes else "Screen never turns off by itself."
    if setting == "mute":
        on = _bool(value)
        run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1" if on else "0"])
        return f"Sound {'muted' if on else 'unmuted'}."
    if setting in ("volume", "brightness"):
        try:
            pct = int(float(str(value).strip().rstrip("%")))
        except ValueError:
            raise ToolError(f"{setting} needs a number 0-100.")
        if setting == "volume":
            pct = max(0, min(pct, 100))
            run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{pct}%"])
            return f"Volume {pct}%."
        pct = max(5, min(pct, 100))  # never fully black: you'd be unable to see to fix it
        _brightness(pct)
        return f"Brightness {pct}%."
    raise ToolError(f"Unknown setting {setting}.")


def lock_screen() -> str:
    """Lock the screen (ROSE keeps running; screenshots will show the lock screen)."""
    run(["gdbus", "call", "--session", "--dest", "org.gnome.ScreenSaver", "--object-path", "/org/gnome/ScreenSaver",
         "--method", "org.gnome.ScreenSaver.SetActive", "true"])
    return "Screen locked."
