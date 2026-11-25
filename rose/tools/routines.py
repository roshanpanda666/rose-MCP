"""Routines: one name runs a list of steps ("work mode", "night mode"). Triggered by voice/chat (run_routine)
or by time (schedule_add kind="routine").

Routines live in ~/rose/routines.json (created with defaults the first time; edit it, or let Rose save new
ones with save_routine). Steps may only use the tools in STEP_TOOLS: nothing that deletes, cuts the network,
powers off or sends messages. A failing step is reported and the rest still run.
"""

from __future__ import annotations

import json
import re
import shutil
import time

from rose.common import ToolError, launch_detached
from rose.config import ROSE_HOME

FILE = ROSE_HOME / "routines.json"

DEFAULTS = {
    "work": {"description": "Work: last VS Code project + a terminal there, work playlist in YouTube Music, "
                            "GroqCloud dashboard, Do Not Disturb, dark style, performance mode",
             "steps": [
                 {"tool": "set_quick_setting", "args": {"setting": "do_not_disturb", "value": "on"}},
                 {"tool": "set_quick_setting", "args": {"setting": "dark_style", "value": "on"}},
                 {"tool": "set_quick_setting", "args": {"setting": "power_mode", "value": "performance"}},
                 {"tool": "open_project", "args": {"name": "recent", "terminal": True}},
                 {"tool": "open_web_app", "args": {
                     "url": "https://music.youtube.com/watch?list=PLUibhYlmTwXjSz_eUjBdWB7uiwD2w2w-V"}},
                 {"tool": "open_url", "args": {"url": "https://console.groq.com/settings/organization/usage"}},
                 {"tool": "wait", "args": {"seconds": 4}},
                 {"tool": "arrange_window", "args": {"window": "Visual Studio Code", "layout": "maximize"}},
             ]},
    "night": {"description": "Wind down: night light, dim screen, quieter, Do Not Disturb, music paused",
              "steps": [
                  {"tool": "set_quick_setting", "args": {"setting": "night_light", "value": "on"}},
                  {"tool": "set_quick_setting", "args": {"setting": "dark_style", "value": "on"}},
                  {"tool": "set_quick_setting", "args": {"setting": "brightness", "value": "30"}},
                  {"tool": "set_quick_setting", "args": {"setting": "volume", "value": "20"}},
                  {"tool": "set_quick_setting", "args": {"setting": "do_not_disturb", "value": "on"}},
                  {"tool": "media_control", "args": {"action": "pause"}},
              ]},
    "break": {"description": "Break: music paused, notifications back on, a reminder to stretch",
              "steps": [
                  {"tool": "media_control", "args": {"action": "pause"}},
                  {"tool": "set_quick_setting", "args": {"setting": "do_not_disturb", "value": "off"}},
                  {"tool": "send_notification", "args": {"title": "Break time", "body": "Stand up, stretch, drink water."}},
              ]},
}


def open_web_app(url: str) -> str:
    """A site in its own app window of the user's signed-in Brave (like the installed web apps)."""
    exe = shutil.which("brave-browser") or "/opt/brave.com/brave/brave-browser"
    launch_detached([exe, "--profile-directory=Default", "--force-renderer-accessibility", f"--app={url}"],
                    name="brave-app")
    return f"opened {url} as an app"


def _step_tools() -> dict:
    from rose.tools import apps, device, notify, projects, quick

    return {"open_project": projects.open_project, "open_web_app": open_web_app,"open_app": apps.open_app, "open_url": apps.open_url, "focus_window": apps.focus_window,
            "arrange_window": apps.arrange_window, "move_window": apps.move_window, "tile_windows": apps.tile_windows,
            "workspace": apps.workspace, "set_quick_setting": quick.set_quick_setting, "lock_screen": quick.lock_screen,
            "media_control": device.media_control, "set_audio_device": device.set_audio_device,
            "set_mic_muted": device.set_mic_muted, "speak": device.speak, "send_notification": notify.send_notification,
            "wait": lambda seconds=2: time.sleep(max(0.0, min(float(seconds), 30))) or f"waited {seconds}s"}


STEP_TOOLS = {"open_app", "open_url", "open_project", "open_web_app", "focus_window", "arrange_window", "move_window", "tile_windows", "workspace",
              "set_quick_setting", "lock_screen", "media_control", "set_audio_device", "set_mic_muted", "speak",
              "send_notification", "wait"}
NO_NETWORK_CUT = {"wifi", "airplane_mode"}
OPENS = {"open_app", "open_url", "open_project", "open_web_app"}
SETTLE = 1.5  # seconds to wait after a routine that opened things, before noting its new windows


def _load() -> dict:
    if not FILE.exists():
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(DEFAULTS, indent=2))
    try:
        return json.loads(FILE.read_text())
    except ValueError:
        raise ToolError(f"{FILE} isn't valid JSON; fix it or delete it to get the defaults back.") from None


def _check(steps: list) -> None:
    if not isinstance(steps, list) or not steps or len(steps) > 25:
        raise ToolError("A routine needs 1-25 steps.")
    for s in steps:
        if not isinstance(s, dict) or s.get("tool") not in STEP_TOOLS:
            raise ToolError(f"Step {s!r} isn't allowed. Steps may use: {', '.join(sorted(STEP_TOOLS))}.")
        if s["tool"] == "set_quick_setting" and (s.get("args") or {}).get("setting") in NO_NETWORK_CUT:
            raise ToolError("Routines can't switch Wi-Fi or airplane mode (that would cut ROSE off).")


def list_routines() -> dict:
    """Routines (name -> what it does and its steps). Edit ~/rose/routines.json or use save_routine."""
    return {name: {"description": r.get("description", ""), "steps": [s["tool"] + " " + json.dumps(s.get("args", {}))
                                                                      for s in r.get("steps", [])]}
            for name, r in _load().items()}


def run_routine(name: str) -> str:
    """Run a routine by name ("work", "night", "break"...): steps in order, failures reported. undo_last reverses
    it (settings back, opened windows closed)."""
    routines = _load()
    key = name.strip().lower().removesuffix(" mode").strip()
    r = routines.get(key)
    if r is None:  # "fire up work mode", "let's do some work" -> the routine named in it
        words = set(re.findall(r"[a-z]+", key))
        found = [k for k in routines if k in words]
        if len(found) == 1:
            key, r = found[0], routines[found[0]]
    if r is None:
        raise ToolError(f"No routine '{name}'. Routines: {', '.join(routines) or 'none'}.")
    _check(r.get("steps", []))
    tools = _step_tools()
    from rose.tools import undo

    try:
        snap = undo.routine_before(r["steps"])
    except Exception:
        snap = None  # never block the routine over the undo snapshot
    done, failed = [], []
    for s in r["steps"]:
        try:
            tools[s["tool"]](**(s.get("args") or {}))
            if s["tool"] != "wait":
                done.append(s["tool"].replace("_", " ") + (f" {s['args'].get('setting', '')}".rstrip()
                                                           if s.get("args") else ""))
        except Exception as e:
            failed.append(f"{s['tool']}: {str(e)[:120]}")
    if snap is not None:
        try:
            if any(st["tool"] in OPENS for st in r["steps"]) and r["steps"][-1]["tool"] != "wait":
                time.sleep(SETTLE)  # let freshly launched windows map, so undo can close them
            undo.routine_after(key, snap)
        except Exception:
            pass
    msg = f"Routine '{key}': {len(done)} step(s) done"
    if failed:
        msg += "; failed: " + "; ".join(failed)
    return msg + "."


def save_routine(name: str, steps_json: str, description: str = "", confirm: bool = False) -> str:
    """Create or replace a routine. steps_json: [{"tool": "...", "args": {...}}, ...]; replacing needs confirm."""
    key = name.strip().lower()
    if not key.replace("-", "").replace("_", "").isalnum() or len(key) > 30:
        raise ToolError("Name: letters/digits, up to 30 characters.")
    try:
        steps = json.loads(steps_json)
    except ValueError:
        raise ToolError("steps_json must be a JSON list of steps.") from None
    _check(steps)
    routines = _load()
    if key in routines and not confirm:
        raise ToolError(f"Routine '{key}' exists. To replace it, ask the user, then call again with confirm=true.")
    routines[key] = {"description": description.strip()[:200], "steps": steps}
    FILE.write_text(json.dumps(routines, indent=2))
    return f"Saved routine '{key}' ({len(steps)} steps)."
