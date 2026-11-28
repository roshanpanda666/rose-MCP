"""Which OS ROSE runs on, and the system command to use for each job on that OS.

The OS comes from the `host_os` setting: "auto" (default) detects it, or the user picks "linux", "windows" or
"macos" in ROSE Desktop's Settings. Callers ask for a job ("open", "play_wav", ...) instead of hardcoding
`xdg-open` or `paplay`, so the same code runs everywhere and says clearly when a job has no command yet.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import psutil

from rose.config import load_config

SUPPORTED = ("linux", "windows", "macos")
LABELS = {"linux": "Linux", "windows": "Windows", "macos": "macOS"}


class Unsupported(RuntimeError):
    """This job has no command on the selected OS (yet)."""


def detected() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def setting() -> str:
    """The saved choice: "auto" or one of SUPPORTED."""
    value = str(load_config().get("host_os", "auto")).strip().lower()
    return value if value in SUPPORTED else "auto"


def current() -> str:
    s = setting()
    return detected() if s == "auto" else s


# Job -> OS -> argv template ("{path}" / "{target}" are filled in). A job missing for an OS is unsupported there.
COMMANDS: dict[str, dict[str, list[str]]] = {
    "open": {
        "linux": ["xdg-open", "{target}"],
        "macos": ["open", "{target}"],
        "windows": ["explorer.exe", "{target}"],
    },
    "play_wav": {
        "linux": ["paplay", "{path}"],
        "macos": ["afplay", "{path}"],
        "windows": ["powershell", "-NoProfile", "-Command",
                    "(New-Object Media.SoundPlayer '{path}').PlaySync()"],
    },
    "record_mic": {
        "linux": ["pw-record", "--rate", "48000", "--channels", "1", "{path}"],
        "macos": ["ffmpeg", "-loglevel", "error", "-y", "-f", "avfoundation", "-i", ":default",
                  "-ac", "1", "-ar", "48000", "{path}"],
    },
    "services": {"linux": ["systemctl", "--user"]},
    "service_logs": {"linux": ["journalctl", "--user"]},
    "desktop_shortcuts": {"linux": ["gsettings"]},
}

# What each job is for, shown in Settings.
PURPOSE = {
    "open": "Open files, folders and links",
    "play_wav": "Test a voice on the laptop speakers",
    "record_mic": "Push-to-talk to the phone",
    "services": "Start/stop ROSE services (remote access, server)",
    "service_logs": "Service logs",
    "desktop_shortcuts": "Keyboard shortcut for the wheel (GNOME)",
}


def command(job: str, os_name: str | None = None, **fill: str) -> list[str]:
    """The argv for a job on the selected OS. Raises Unsupported if that OS has none."""
    osn = os_name or current()
    tpl = COMMANDS[job].get(osn)
    if not tpl:
        raise Unsupported(f"{PURPOSE.get(job, job)}: not available on {LABELS[osn]} yet.")
    return [part.format(**fill) for part in tpl]


def supports(job: str, os_name: str | None = None) -> bool:
    return (os_name or current()) in COMMANDS[job]


def _detach(argv: list[str]) -> None:
    kw: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(argv, **kw)


def open_target(target: str) -> None:
    """Open a file, folder or http(s) link with the desktop's default app."""
    if current() == "windows" and hasattr(os, "startfile"):
        os.startfile(target)  # noqa: S606 - the Windows way to "open with default app"
        return
    _detach(command("open", target=target))


def detach(*argv: str) -> None:
    """Start a program in the background, detached from us."""
    _detach(list(argv))


def process_running(needle: str) -> bool:
    """True if some process's command line contains `needle` (replaces `pgrep -f`)."""
    me = os.getpid()
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            if p.info["pid"] != me and needle in " ".join(p.info["cmdline"] or []):
                return True
        except (psutil.Error, TypeError):
            continue
    return False


def info() -> dict:
    """For Settings: the choice, what was detected, and each job's command and whether it is installed."""
    osn = current()
    jobs = []
    for job, per_os in COMMANDS.items():
        tpl = per_os.get(osn)
        exe = tpl[0] if tpl else ""
        jobs.append({"job": job, "purpose": PURPOSE.get(job, job), "command": " ".join(tpl[:2]) if tpl else "",
                     "supported": bool(tpl), "installed": bool(exe and shutil.which(exe))})
    return {"setting": setting(), "detected": detected(), "current": osn,
            "options": [{"value": "auto", "label": f"Automatic ({LABELS[detected()]})"}]
            + [{"value": k, "label": v} for k, v in LABELS.items()],
            "mismatch": osn != detected(), "jobs": jobs}
