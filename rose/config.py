"""ROSE configuration.

* Settings you edit: ~/.config/rose/config.toml (override with ROSE_CONFIG). Created with defaults on
  first run; an older ~/rose/config.json is migrated into it automatically.
* Data ROSE writes: ~/rose/ (override with ROSE_HOME): logs, screenshots, webcam, bridge, auth.
* Runtime state ROSE writes: ~/rose/runtime.json (currently just the tunnel's public URL), so ROSE
  never rewrites the file you edit by hand.

Settings are re-read on every call, so edits apply without a restart (except port/public URL).
"""

from __future__ import annotations

import copy
import json
import os
import threading
import tomllib
from pathlib import Path
from typing import Any

ROSE_HOME = Path(os.environ.get("ROSE_HOME", Path.home() / "rose")).expanduser()
CONFIG_PATH = Path(os.environ.get("ROSE_CONFIG", Path.home() / ".config" / "rose" / "config.toml")).expanduser()
LEGACY_CONFIG_PATH = ROSE_HOME / "config.json"
RUNTIME_PATH = ROSE_HOME / "runtime.json"
SCREENSHOT_DIR = ROSE_HOME / "screenshots"
LOG_DIR = ROSE_HOME / "logs"
WORKSPACE_DIR = ROSE_HOME / "workspace"

DEFAULT_CONFIG = {
    "http_port": 8765,
    "tunnel_provider": "cloudflare",
    "ngrok_domain": "",
    "allowed_dirs": [str(WORKSPACE_DIR), "~/Documents", "~/Downloads", "~/Desktop"],
    "max_read_bytes": 1_000_000,
    "input_enabled": True,
    "webcam_device": "",
    "bridge_path": "",
    "bridge_email_enabled": True,
    "bridge_email_to": [],
    "oauth_redirect_hosts": ["claude.ai", "claude.com", "localhost", "127.0.0.1"],
    "forge_enabled": True,
    "forge_model": "",
    "forge_timeout": 600,
    "forge_max_running": 2,
    "forge_max_per_hour": 20,
    "approval_mode": "passphrase",
    "approval_required_tools": ["webcam_capture"],
    "approval_grant_minutes": 60,
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    "push_enabled": True,
    "watch_enabled": True,
    "alert_disk_percent": 90,
    "alert_ram_percent": 92,
    "alert_battery_percent": 15,
    "keep_screenshots_days": 7,
    "keep_logs_days": 30,
    "max_output_chars": 12000,
    "large_output_chars": 4000,
    "disabled_tools": [],
    "live_view_min_side": 1920,
    "live_view_min_quality": 75,
    "commands": {},
}

_COMMENTS = {
    "http_port": "Port for `rose serve` (always 127.0.0.1; the tunnel is the only way in from outside)",
    "tunnel_provider": 'Public tunnel: "ngrok" (permanent URL) or "cloudflare" (quick tunnel, URL changes)',
    "ngrok_domain": 'Your free ngrok static domain, e.g. "abc-def-123.ngrok-free.app"',
    "allowed_dirs": "Folders the file tools may read and write",
    "max_read_bytes": "read_file and content search skip files bigger than this",
    "input_enabled": "Set false to turn off mouse and keyboard control",
    "webcam_device": 'Default camera for webcam_capture, e.g. "/dev/video0" ("" = first camera)',
    "bridge_path": 'Bridge markdown file ("" = ~/rose/bridge/BRIDGE.md)',
    "oauth_redirect_hosts": "OAuth clients may only redirect back to these hosts",
    "bridge_email_enabled": "Email a copy of every bridge post (sender set up with `rose email setup`)",
    "bridge_email_to": "Who gets bridge emails",
    "forge_enabled": "Allow forge_run (headless Claude Code on the work account)",
    "forge_model": 'Model for headless Forge ("" = account default; e.g. "sonnet" is cheaper)',
    "forge_timeout": "Seconds before a Forge job is killed",
    "forge_max_running": "Forge jobs allowed at the same time",
    "forge_max_per_hour": "Forge jobs allowed per hour",
    "approval_mode": ('"passphrase": remote risky actions need an approval link + your passphrase '
                      '(recommended). "chat": confirm=true in chat is enough'),
    "approval_required_tools": "Tools that always need an approval link when used remotely",
    "approval_grant_minutes": "After you approve once, that client can act without asking again for this many minutes (0 = ask every time)",
    "ntfy_server": "Push server for phone notifications (ntfy)",
    "ntfy_topic": "Secret ntfy topic your phone subscribes to (set by `rose push setup`)",
    "push_enabled": "Send alerts to your phone",
    "watch_enabled": "Run watchers and schedules (rose-watch.service)",
    "alert_disk_percent": "Alert when the disk is this full",
    "alert_ram_percent": "Alert when RAM stays this full",
    "alert_battery_percent": "Alert when unplugged and the battery drops to this",
    "keep_screenshots_days": "Delete screenshots and webcam photos older than this",
    "keep_logs_days": "Delete daily logs older than this",
    "max_output_chars": "Cap on the text a tool returns to the model (longer output is cut with a note; 0 = no cap)",
    "large_output_chars": "Log tool outputs bigger than this to ~/rose/logs/output-*.jsonl (see `rose tokens`)",
    "disabled_tools": 'Tools ROSE should not offer at all, e.g. ["webcam_capture", "lock_screen"]',
    "live_view_min_side": "Live screen view (screen_frame): frames are at least this many px on the long side (1920 = native, sharpest)",
    "live_view_min_quality": "Live screen view: minimum JPEG quality (30-90; higher = sharper text, bigger frames)",
    "commands": ('Command presets Rose can run by name (no shell, no arguments), e.g.\n'
                 '# notes-pull = ["git", "-C", "~/notes", "pull"]\n# disk = ["df", "-h", "/"]'),
}


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v)  # JSON string escaping is valid TOML basic-string escaping
    if isinstance(v, list):
        return "[\n" + "".join(f"  {_toml_value(x)},\n" for x in v) + "]"
    raise TypeError(f"Can't write {type(v).__name__} to TOML")


def write_default_config(values: dict) -> None:
    lines = ["# ROSE settings. Auth secrets are NOT here: manage them with `rose auth`.", ""]
    tables = {k: d for k, d in DEFAULT_CONFIG.items() if isinstance(d, dict)}
    for key, default in DEFAULT_CONFIG.items():
        if key in tables:
            continue
        lines.append(f"# {_COMMENTS[key]}")
        lines.append(f"{key} = {_toml_value(values.get(key, default))}")
        lines.append("")
    for key in tables:  # TOML tables must come after all top-level keys
        lines.append(f"# {_COMMENTS[key]}")
        lines.append(f"[{key}]")
        for name, argv in (values.get(key) or {}).items():
            lines.append(f"{json.dumps(name)} = {_toml_value(argv)}")
        lines.append("")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text("\n".join(lines))


def _ensure() -> None:
    for d in (ROSE_HOME, SCREENSHOT_DIR, LOG_DIR, WORKSPACE_DIR):
        d.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        return
    values: dict = {}
    if LEGACY_CONFIG_PATH.exists():  # migrate v1/v2 config.json
        legacy = json.loads(LEGACY_CONFIG_PATH.read_text())
        values = {k: v for k, v in legacy.items() if k in DEFAULT_CONFIG}
        if legacy.get("public_url"):
            set_public_url(legacy["public_url"])
        LEGACY_CONFIG_PATH.rename(LEGACY_CONFIG_PATH.with_suffix(".json.migrated"))
    write_default_config(values)


_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"key": None, "cfg": None}


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def load_config() -> dict:
    """Current settings (defaults + config.toml + the tunnel's public URL).

    Called on every tool call, so the parsed result is cached and re-read only when config.toml or
    runtime.json changes on disk. Returns a fresh copy: callers may modify it freely."""
    _ensure()
    key = (_stamp(CONFIG_PATH), _stamp(RUNTIME_PATH))
    with _cache_lock:
        if _cache["key"] != key:
            with open(CONFIG_PATH, "rb") as f:
                data = tomllib.load(f)
            cfg = {**DEFAULT_CONFIG, **data}
            cfg["public_url"] = get_public_url()
            _cache.update(key=key, cfg=cfg)
        return copy.deepcopy(_cache["cfg"])


def update_settings(changes: dict) -> None:
    """Change some settings (used by the dashboard). Rewrites config.toml with all current values."""
    unknown = set(changes) - set(DEFAULT_CONFIG)
    if unknown:
        raise KeyError(f"Unknown settings: {unknown}")
    cfg = load_config()
    cfg.pop("public_url", None)
    cfg.update(changes)
    write_default_config(cfg)


def get_public_url() -> str:
    try:
        return json.loads(RUNTIME_PATH.read_text()).get("public_url", "")
    except (OSError, ValueError):
        return ""


def set_public_url(url: str) -> None:
    ROSE_HOME.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(RUNTIME_PATH.read_text())
    except (OSError, ValueError):
        state = {}
    state["public_url"] = url
    tmp = RUNTIME_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(RUNTIME_PATH)


def allowed_dirs() -> list[Path]:
    """Allowlisted roots, resolved. Re-read on every call so edits apply without a restart."""
    return [Path(p).expanduser().resolve() for p in load_config()["allowed_dirs"]]
