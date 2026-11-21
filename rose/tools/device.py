"""Direct device control by command (no screenshots or mouse): media, audio devices, Bluetooth devices,
Wi-Fi networks, speech, opening files, stopping processes, and user-defined command presets.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from rose.common import ToolError, display_env, run, run_text
from rose.config import load_config

MAX_OUTPUT = 8000
_out = run_text


# ---------------- media (MPRIS over D-Bus, works with Spotify, browsers, VLC, ...) ----------------

def _players() -> list[str]:
    names = _out(["busctl", "--user", "list", "--no-legend"]).splitlines()
    return [l.split()[0] for l in names if l.startswith("org.mpris.MediaPlayer2.")]


def _mpris_get(player: str, prop: str) -> str:
    return _out(["gdbus", "call", "--session", "--dest", player, "--object-path", "/org/mpris/MediaPlayer2",
                 "--method", "org.freedesktop.DBus.Properties.Get", "org.mpris.MediaPlayer2.Player", prop])


def _player_info(p: str) -> dict:
    status = re.search(r"'(\w+)'", _mpris_get(p, "PlaybackStatus"))
    meta = _mpris_get(p, "Metadata")
    title = re.search(r"'xesam:title': <'((?:[^'\\]|\\.)*)'>", meta)
    artist = re.search(r"'xesam:artist': <\['((?:[^'\\]|\\.)*)'", meta)
    return {"player": p.removeprefix("org.mpris.MediaPlayer2.").split(".")[0], "bus": p,
            "status": status.group(1) if status else "?", "title": title.group(1) if title else None,
            "artist": artist.group(1) if artist else None}


def media_control(action: Literal["status", "play_pause", "play", "pause", "next", "previous", "stop"] = "status",
                  player: str = "") -> dict | str:
    """Media playback (Spotify, browser, VLC...): status/play/pause/next... Default: the playing player."""
    buses = _players()
    with ThreadPoolExecutor(max_workers=max(1, len(buses))) as pool:
        players = list(pool.map(_player_info, buses))
    if not players:
        return "Nothing is playing and no media player is open."
    if action == "status":
        return {"players": [{k: v for k, v in p.items() if k != "bus"} for p in players]}
    target = next((p for p in players if player and player.lower() in p["player"].lower()), None) if player else \
        next((p for p in players if p["status"] == "Playing"), players[0])
    if target is None:
        raise ToolError(f"No player matches '{player}'. Open players: {[p['player'] for p in players]}")
    method = {"play_pause": "PlayPause", "play": "Play", "pause": "Pause", "next": "Next",
              "previous": "Previous", "stop": "Stop"}[action]
    run(["gdbus", "call", "--session", "--dest", target["bus"], "--object-path", "/org/mpris/MediaPlayer2",
         "--method", f"org.mpris.MediaPlayer2.Player.{method}"])
    return f"{method} on {target['player']}" + (f" ({target['title']})" if target["title"] else "") + "."


# ---------------- audio devices ----------------

def _wpctl_section(kind: str) -> list[dict]:
    text = _out(["wpctl", "status"])
    audio = text.split("Audio", 1)[1].split("Video", 1)[0] if "Audio" in text else text
    block = audio.split(f"{kind}:", 1)[1] if f"{kind}:" in audio else ""
    rows = []
    for line in block.splitlines()[1:]:
        if re.search(r"(Sinks|Sources|Filters|Streams):", line):
            break
        m = re.search(r"(\*?)\s*(\d+)\.\s+(.+?)(?:\s+\[vol:.*\])?\s*$", line)
        if m:
            rows.append({"id": int(m.group(2)), "name": m.group(3).strip(), "default": m.group(1) == "*"})
    return rows


def audio_devices() -> dict:
    """Audio outputs and inputs, with defaults."""
    return {"outputs": _wpctl_section("Sinks"), "inputs": _wpctl_section("Sources")}


def set_audio_device(device: str, kind: Literal["output", "input"] = "output") -> str:
    """Set default output/input device by id or name part."""
    devs = _wpctl_section("Sinks" if kind == "output" else "Sources")
    d = device.strip().lower()
    hits = [x for x in devs if d == str(x["id"]) or d in x["name"].lower()]
    if len(hits) != 1:
        raise ToolError(f"{'No' if not hits else 'Several'} {kind}s match '{device}': "
                        f"{[x['name'] for x in (hits or devs)]}")
    run(["wpctl", "set-default", str(hits[0]["id"])])
    return f"Default {kind}: {hits[0]['name']}."


def set_mic_muted(muted: bool) -> str:
    """Mute/unmute the default mic."""
    run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "1" if muted else "0"])
    return f"Microphone {'muted' if muted else 'unmuted'}."


# ---------------- Bluetooth devices ----------------

def _bt(*args: str, timeout: float = 15) -> str:
    return subprocess.run(["bluetoothctl", *args], capture_output=True, text=True, timeout=timeout,
                          env=display_env()).stdout


def bluetooth_devices() -> list[dict]:
    """Paired Bluetooth devices and connection state."""
    devs = [m.groups() for line in _bt("devices", "Paired").splitlines()
            if (m := re.match(r"Device ([0-9A-F:]{17}) (.+)", line.strip()))]
    with ThreadPoolExecutor(max_workers=max(1, len(devs))) as pool:
        infos = list(pool.map(lambda d: _bt("info", d[0]), devs))
    return [{"name": name, "address": addr, "connected": "Connected: yes" in info}
            for (addr, name), info in zip(devs, infos, strict=True)]


def bluetooth_connect(device: str, connect: bool = True) -> str:
    """Connect/disconnect a paired Bluetooth device (name part or address)."""
    devs = bluetooth_devices()
    d = device.strip().lower()
    hits = [x for x in devs if d == x["address"].lower() or d in x["name"].lower()]
    if len(hits) != 1:
        raise ToolError(f"{'No' if not hits else 'Several'} paired devices match '{device}': {[x['name'] for x in (hits or devs)]}")
    dev = hits[0]
    if connect:
        from rose.tools.quick import set_quick_setting
        set_quick_setting("bluetooth", "on")
        _bt("power", "on")
        out = _bt("connect", dev["address"], timeout=25)
        ok = "Connection successful" in out or any(x["connected"] for x in bluetooth_devices() if x["address"] == dev["address"])
        if not ok:
            raise ToolError(f"Couldn't connect {dev['name']}. Is it switched on and nearby? {out.strip()[-160:]}")
        return f"Connected {dev['name']}."
    _bt("disconnect", dev["address"])
    return f"Disconnected {dev['name']}."


# ---------------- Wi-Fi networks ----------------

def wifi_networks() -> dict:
    """Wi-Fi in range (strongest first) and saved networks."""
    visible = []
    for line in _out(["nmcli", "-t", "-f", "IN-USE,SIGNAL,SECURITY,SSID", "dev", "wifi", "list"]).splitlines():
        parts = line.split(":", 3)
        if len(parts) == 4 and parts[3]:
            visible.append({"ssid": parts[3].replace("\\:", ":"), "signal": int(parts[1] or 0),
                            "secured": parts[2] not in ("", "--"), "connected": parts[0] == "*"})
    saved = [l.split(":")[0] for l in _out(["nmcli", "-t", "-f", "NAME,TYPE", "con", "show"]).splitlines()
             if l.endswith(":802-11-wireless")]
    return {"in_range": sorted(visible, key=lambda x: -x["signal"])[:20], "saved": saved}


def wifi_connect(network: str, confirm: bool = False) -> str:
    """Switch to a saved Wi-Fi network. Needs confirm=true (drops ROSE's link; reverts after 90 s if offline)."""
    lines = _out(["nmcli", "-t", "-f", "NAME,UUID,TYPE,ACTIVE", "con", "show"]).splitlines()
    saved = [dict(zip(("name", "uuid", "type", "active"), line.rsplit(":", 3), strict=False)) for line in lines]
    saved = [s for s in saved if s.get("type") == "802-11-wireless"]
    hit = [s for s in saved if s["name"].lower() == network.strip().lower()] or \
          [s for s in saved if network.strip().lower() in s["name"].lower()]
    if len(hit) != 1:
        raise ToolError(f"{'No' if not hit else 'Several'} saved networks match '{network}': {[s['name'] for s in (hit or saved)]}. "
                        "ROSE only switches to networks already saved on this laptop.")
    target = hit[0]
    if target["active"] == "yes":
        return f"Already connected to {target['name']}."
    if not confirm:
        raise ToolError(f"Switching to {target['name']} drops the current connection (ROSE is unreachable for a "
                        "moment). Ask the user, then call again with confirm=true.")
    current = next((s for s in saved if s["active"] == "yes"), None)
    from rose.tools.quick import _schedule
    rose_bin = os.path.join(os.path.dirname(sys.executable), "rose")
    if current and os.path.exists(rose_bin):  # safety net: come back if the new network has no internet
        _schedule([rose_bin, "net-fallback", current["uuid"]], 90, "wifi-fallback")
    _schedule(["nmcli", "con", "up", "uuid", target["uuid"]], 3, "wifi-switch")
    return (f"Switching to {target['name']} in 3s. If it has no internet after 90s, the laptop goes back to "
            f"{current['name'] if current else 'the previous network'}.")


def net_fallback(uuid: str) -> int:
    """CLI helper (scheduled by wifi_connect): reconnect `uuid` if there's no full connectivity."""
    if not re.fullmatch(r"[0-9a-f-]{36}", uuid):
        return 1
    state = subprocess.run(["nmcli", "-t", "-f", "CONNECTIVITY", "g"], capture_output=True, text=True).stdout.strip()
    if state != "full":
        subprocess.run(["nmcli", "con", "up", "uuid", uuid])
    return 0


# ---------------- speech, files, processes ----------------

def speak(text: str) -> str:
    """Speak text aloud (TTS)."""
    text = text.strip()
    if not text or len(text) > 500:
        raise ToolError("Text must be 1-500 characters.")
    run(["spd-say", "--wait", "--", text], timeout=60)
    return "Spoken."


def open_path(path: str) -> str:
    """Open an allowed file/folder in its default app."""
    from rose.tools.files import _resolve
    p = _resolve(path)
    if not p.exists():
        raise ToolError(f"{p} doesn't exist.")
    subprocess.Popen(["xdg-open", str(p)], env=display_env(), stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return f"Opened {p}."


PROTECTED = {"rose", "ngrok", "cloudflared", "systemd", "gnome-shell", "Xorg", "Xwayland", "pipewire",
             "wireplumber", "NetworkManager", "dbus-daemon", "gnome-session-binary", "claude"}


def stop_process(target: str, confirm: bool = False) -> str:
    """SIGTERM one of your processes by pid or name. Needs confirm=true; system/ROSE/Claude protected."""
    import psutil

    me = psutil.Process().username()
    t = target.strip()
    procs = [psutil.Process(int(t))] if t.isdigit() else \
        [p for p in psutil.process_iter(["name", "username"]) if p.info["name"] and t.lower() == p.info["name"].lower()]
    procs = [p for p in procs if p.is_running()]
    if not procs:
        raise ToolError(f"No running process '{target}'. Use system_status to see the top processes.")
    for p in procs:
        name = p.name()
        if p.username() != me or p.pid in (os.getpid(), os.getppid()) or name in PROTECTED \
                or any("rose" in part for part in p.cmdline()[:3]):
            raise ToolError(f"Refusing to stop {name} (pid {p.pid}): it's protected or not your process.")
    if not confirm:
        mem = sum(p.memory_info().rss for p in procs) / 1024**2
        raise ToolError(f"This would stop {len(procs)} process(es) named {procs[0].name()} ({mem:.0f} MB). "
                        "Unsaved work in it may be lost. Ask the user, then call again with confirm=true.")
    for p in procs:
        p.send_signal(signal.SIGTERM)
    return f"Asked {len(procs)} {procs[0].name()} process(es) to quit."


# ---------------- user-defined command presets ----------------

def _presets() -> dict[str, list[str]]:
    cmds = load_config().get("commands", {}) or {}
    return {k: v for k, v in cmds.items() if isinstance(v, list) and v and all(isinstance(a, str) for a in v)}


def list_commands() -> dict:
    """User command presets ([commands] in config.toml)."""
    return {name: " ".join(argv) for name, argv in _presets().items()} or \
        {"(none)": "Add presets under [commands] in ~/.config/rose/config.toml"}


def run_command(name: str) -> dict:
    """Run a preset by name (fixed command, no arguments)."""
    presets = _presets()
    if name not in presets:
        raise ToolError(f"No preset '{name}'. Available: {list(presets) or 'none'}")
    argv = [os.path.expanduser(a) for a in presets[name]]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=display_env(),
                           cwd=Path.home(), stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise ToolError(f"'{argv[0]}' isn't installed or not on PATH.") from None
    except subprocess.TimeoutExpired:
        raise ToolError("The command took longer than 60s and was stopped.") from None
    out = (p.stdout + ("\n" + p.stderr if p.stderr.strip() else "")).strip()
    return {"exit_code": p.returncode, "output": out[-MAX_OUTPUT:] if len(out) > MAX_OUTPUT else out}
