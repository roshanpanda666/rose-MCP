"""Context-triggered routines, checked by rose-watch every tick.

Config `context_triggers` is a list of {"when": ..., "routine": name, "ask": bool, "cooldown_minutes": n}:
  "monitor_connected" / "monitor_disconnected"   the number of monitors went up / down
  "wifi:<SSID>"                                   joined that Wi-Fi network
  "app_opened:<name>"                             a window of that app appeared (app class or title contains it)
Fires on transitions only (never on the first tick after start), with a per-trigger cooldown.
ask=true pushes a question whose link opens the phone app's confirm button; ask=false runs the routine.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime

from rose.common import _log, display_env
from rose.config import load_config
from rose.push import push

DEFAULT_TRIGGERS = [{"when": "monitor_connected", "routine": "work", "ask": True}]
DEFAULT_PHONE_URL = "https://debian.tail41061c.ts.net"
DEFAULT_COOLDOWN_MIN = 30
_WHEN = re.compile(r"^(monitor_connected|monitor_disconnected|wifi:.+|app_opened:.+)$")

_prev: dict | None = None  # last observation in this process (None = first tick: just observe)


def _triggers() -> list[dict]:
    raw = load_config().get("context_triggers", DEFAULT_TRIGGERS)
    out = []
    for t in raw if isinstance(raw, list) else []:
        if isinstance(t, dict) and _WHEN.match(str(t.get("when", "")).strip()) and str(t.get("routine", "")).strip():
            out.append({"when": str(t["when"]).strip(), "routine": str(t["routine"]).strip().lower(),
                        "ask": bool(t.get("ask", True)),
                        "cooldown_minutes": float(t.get("cooldown_minutes", DEFAULT_COOLDOWN_MIN))})
    return out


def _key(t: dict) -> str:
    return f"{t['when']}->{t['routine']}"


# ---------------- observations ----------------

def monitor_count() -> int | None:
    try:
        from rose.tools.screenshot import monitors
        mons = monitors()
        return len(mons) or None  # [] = unknown (no X/xrandr), not "zero monitors"
    except Exception:
        return None


def wifi_ssid() -> str | None:
    """SSID of the connected Wi-Fi ('' = not on Wi-Fi), None if unknown."""
    if not shutil.which("nmcli"):
        return None
    try:
        out = subprocess.run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi", "list", "--rescan", "no"],
                             capture_output=True, text=True, timeout=5, env=display_env())
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        if line.startswith("yes:"):
            return line[4:].replace("\\:", ":")
    return ""


def open_apps() -> list[str] | None:
    """Lowercased 'app title' strings of open windows, None if unknown."""
    try:
        from rose.tools.apps import list_windows
        return [f"{w.get('app', '')} {w.get('title', '')}".lower() for w in list_windows()]
    except Exception:
        return None


def observe(triggers: list[dict]) -> dict:
    """Only look at what the configured triggers need."""
    kinds = {t["when"].split(":")[0] for t in triggers}
    obs: dict = {}
    if kinds & {"monitor_connected", "monitor_disconnected"}:
        obs["monitors"] = monitor_count()
    if "wifi" in kinds:
        obs["wifi"] = wifi_ssid()
    if "app_opened" in kinds:
        obs["apps"] = open_apps()
    return obs


def _app_present(apps: list[str] | None, name: str) -> bool | None:
    if apps is None:
        return None
    n = name.lower()
    return any(n in a for a in apps)


def fired(t: dict, prev: dict, cur: dict) -> str:
    """Why trigger `t` fires on prev -> cur ('' = it doesn't)."""
    when = t["when"]
    if when in ("monitor_connected", "monitor_disconnected"):
        a, b = prev.get("monitors"), cur.get("monitors")
        if a is None or b is None:
            return ""
        if when == "monitor_connected" and b > a:
            return f"Monitor connected ({a} -> {b})"
        if when == "monitor_disconnected" and b < a:
            return f"Monitor disconnected ({a} -> {b})"
        return ""
    kind, _, arg = when.partition(":")
    if kind == "wifi":
        a, b = prev.get("wifi"), cur.get("wifi")
        if a is None or b is None:
            return ""
        return f"Joined Wi-Fi {arg}" if b == arg and a != arg else ""
    if kind == "app_opened":
        a, b = _app_present(prev.get("apps"), arg), _app_present(cur.get("apps"), arg)
        return f"{arg} opened" if a is False and b is True else ""
    return ""


# ---------------- firing ----------------

def _fire(t: dict, reason: str) -> None:
    from rose import guard
    name = t["routine"]
    if guard.lockdown():
        _log({"event": "trigger_skipped_lockdown", "when": t["when"], "routine": name})
        return
    if t["ask"]:
        base = str(load_config().get("phone_app_url", DEFAULT_PHONE_URL)).rstrip("/")
        push(f"Start {name} mode?", f"{reason}. Tap to run routine '{name}'.", url=f"{base}/?do=routine:{name}",
             tags="question")
    else:
        from rose.tools import routines
        try:
            msg = routines.run_routine(name)
        except Exception as e:
            msg = f"Routine '{name}' failed: {str(e)[:200]}"
        push("🔁 " + reason, msg, tags="repeat")
    _log({"event": "trigger_fired", "when": t["when"], "routine": name, "ask": t["ask"]})


def tick(state: dict, now: datetime | None = None) -> list[str]:
    """Watch-loop hook. Returns the trigger keys that fired."""
    global _prev
    triggers = _triggers()
    if not triggers:
        _prev = None
        return []
    cur = observe(triggers)
    prev, _prev = _prev, cur
    if prev is None:  # first tick after start (or after triggers were added): observe only
        return []
    now = now or datetime.now()
    last = state.setdefault("triggers_last_fired", {})
    out = []
    for t in triggers:
        reason = fired(t, prev, cur)
        if not reason:
            continue
        k = _key(t)
        try:
            ago = (now - datetime.fromisoformat(last[k])).total_seconds() if k in last else None
        except ValueError:
            ago = None
        if ago is not None and ago < t["cooldown_minutes"] * 60:
            continue
        last[k] = now.isoformat(timespec="seconds")
        _fire(t, reason)
        out.append(k)
    return out


def list_triggers() -> list[dict]:
    """Context triggers (monitor connected, Wi-Fi joined, app opened -> routine), whether they ask first, and
    when each last fired. Edit context_triggers in config.toml."""
    from rose.watch import STATE, _load
    last = _load(STATE, {}).get("triggers_last_fired", {})
    try:
        from rose.tools.routines import list_routines
        known = set(list_routines())
    except Exception:
        known = None
    out = []
    for t in _triggers():
        row = {"when": t["when"], "routine": t["routine"], "ask": t["ask"],
               "cooldown_minutes": t["cooldown_minutes"], "last_fired": last.get(_key(t))}
        if known is not None and t["routine"] not in known:
            row["warning"] = f"No routine '{t['routine']}'."
        out.append(row)
    if not load_config().get("watch_enabled", True):
        out.append({"note": "watch_enabled is false: triggers are not checked."})
    return out or [{"none": "No context triggers (config context_triggers)."}]
