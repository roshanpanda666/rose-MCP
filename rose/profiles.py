"""Tool profiles: each client sees only the tools it needs (smaller tool list = fewer tokens per request).

Profiles are lists of tool names or glob patterns ("phone_*"). Built in:
  dev      code work: files, git, projects, Claude Code handoff, GitHub, Docker, Forge, FreeLLMAPI
  phone    the Android phone plus light laptop control (media, volume, apps, screen, routines)
  desktop  laptop control: apps, windows, input, accessibility tree, browser, audio, Wi-Fi, recording
  full     every tool (the default, so nothing changes until you pick profiles)
Every profile also gets CORE (approvals, undo, memory, notifications, today's agenda, ...).

Which profile a client gets, first match wins:
  1. env ROSE_PROFILE (stdio: `rose stdio --profile dev`; applies to every client of that process)
  2. config client_profiles = {"<client label>" = "<profile>"}  (labels as in `rose auth list`, e.g.
     "Phone app", "Forge", "Claude (personal)")
  3. config default_profile (default "full")
Config tool_profiles = {name = [patterns]} adds a profile or replaces a built-in one.
"""

from __future__ import annotations

import fnmatch
import os

from rose.common import ToolError

FULL = "full"

CORE = [
    "check_approval", "undo_last", "undo_history", "memory_save", "memory_search", "memory_list", "memory_forget",
    "send_notification", "system_status", "list_allowed_dirs", "list_dir", "read_file", "search_files",
    "clipboard_read", "clipboard_write", "clipboard_history", "bridge_read", "bridge_post", "today_overview",
    "failure_report", "watch_status", "observation_stats",
]

DEV = [
    "write_file", "move_file", "create_folder", "copy_file", "trash_file", "edit_file", "zip_files", "unzip_file",
    "list_projects", "open_project", "open_path", "new_project", "hand_to_claude", "claude_job_status",
    "git_*", "project_*", "explain_error", "docker", "github_*", "list_commands", "run_command", "stop_process",
    "activity_today", "standup", "forge_*", "freellm_*", "package_updates", "screen_ocr", "take_screenshot",
    "list_windows", "focus_window", "open_url", "open_app", "list_apps", "list_triggers",
]

PHONE = [
    "phone_*", "send_to_phone", "speak", "media_control", "audio_devices", "set_audio_device", "set_mic_muted",
    "quick_settings", "set_quick_setting", "lock_screen", "power_control", "open_app", "open_url", "list_apps",
    "list_windows", "focus_window", "close_window", "take_screenshot", "screen_frame", "screen_live_*",
    "screen_ocr", "click_on_screen", "find_on_screen", "screen_elements", "ui_list", "ui_read", "ui_click",
    "ui_fill", "press_keys", "type_text", "webcam_*", "list_cameras", "mic_*", "motion_*", "recording_*",
    "run_routine", "list_routines", "schedule_*", "open_project", "list_projects", "hand_to_claude",
    "claude_job_status", "activity_today", "standup", "browser_bookmarks",
]

DESKTOP = [
    "take_screenshot", "list_monitors", "mouse_*", "scroll", "type_text", "press_keys", "click_on_screen",
    "find_on_screen", "screen_elements", "screen_frame", "screen_live_*", "screen_ocr", "ui_*", "browser_*",
    "list_apps", "open_app", "open_url", "open_path", "list_windows", "focus_window", "close_window",
    "move_window", "arrange_window", "tile_windows", "workspace", "quick_settings", "set_quick_setting",
    "lock_screen", "power_control", "media_control", "audio_devices", "set_audio_device", "set_mic_muted",
    "bluetooth_*", "wifi_*", "speak", "stop_process", "list_commands", "run_command", "webcam_*", "list_cameras",
    "mic_*", "motion_*", "recording_*", "run_routine", "list_routines", "save_routine", "schedule_*",
    "list_triggers", "write_file", "move_file", "create_folder", "copy_file", "trash_file", "edit_file",
    "zip_files", "unzip_file", "sort_downloads", "package_updates", "open_project", "list_projects",
    "activity_today", "standup", "rose_maintenance", "push_status",
]

BUILTIN: dict[str, list[str]] = {"dev": DEV, "phone": PHONE, "desktop": DESKTOP}


def profiles() -> dict[str, list[str]]:
    """Every profile except full: built-ins, overridden/extended by config tool_profiles."""
    from rose.config import load_config
    out = {k: list(v) for k, v in BUILTIN.items()}
    extra = load_config().get("tool_profiles") or {}
    if isinstance(extra, dict):
        for name, pats in extra.items():
            if isinstance(pats, list) and str(name).strip().lower() != FULL:
                out[str(name).strip().lower()] = [str(p) for p in pats]
    return out


def names() -> list[str]:
    return [*profiles(), FULL]


def expand(profile: str, all_tools: list[str]) -> set[str] | None:
    """Tool names in `profile` (None = every tool). Unknown profile -> ToolError (never silently 'full')."""
    profile = (profile or FULL).strip().lower()
    if profile == FULL:
        return None
    table = profiles()
    if profile not in table:
        raise ToolError(f"Unknown tool profile {profile!r}. Known: {', '.join(names())}.")
    pats = CORE + table[profile]
    return {t for t in all_tools if any(fnmatch.fnmatchcase(t, p) for p in pats)}


def current_profile() -> str:
    """Profile for the client making this request (see module docstring)."""
    env = os.environ.get("ROSE_PROFILE", "").strip()
    if env:
        return env.lower()
    from rose.config import load_config
    cfg = load_config()
    try:
        from rose.tools.bridge import caller
        who = caller()
    except Exception:
        who = ""
    by_client = cfg.get("client_profiles") or {}
    if isinstance(by_client, dict) and who in by_client:
        return str(by_client[who]).strip().lower()
    return str(cfg.get("default_profile") or FULL).strip().lower()


def apply(mcp) -> None:
    """Filter tools/list and refuse tools/call outside the caller's profile (wraps the server's methods)."""
    list_all, call = mcp.list_tools, mcp.call_tool

    async def list_tools():
        tools = await list_all()
        allowed = expand(current_profile(), [t.name for t in tools])
        return tools if allowed is None else [t for t in tools if t.name in allowed]

    async def call_tool(name, arguments, context=None):
        prof = current_profile()
        allowed = expand(prof, [t.name for t in mcp._tool_manager.list_tools()])
        if allowed is not None and name not in allowed:
            raise ToolError(f"Tool {name!r} is not in this client's tool profile '{prof}'. Use a tool from the "
                            f"list, or ask the user to change client_profiles in config.toml.")
        return await call(name, arguments, context)

    mcp.list_tools, mcp.call_tool = list_tools, call_tool


def summary(all_tools: list[str]) -> dict[str, int]:
    """Tool count per profile (for `rose profiles`)."""
    return {p: len(all_tools) if p == FULL else len(expand(p, all_tools) or ()) for p in names()}
