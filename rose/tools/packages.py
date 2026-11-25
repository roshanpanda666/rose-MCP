"""Pending software updates: apt (from the last `apt update`), uv tools, flatpak. Upgrading opens a terminal
where the user types their sudo password; ROSE never runs sudo itself."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Literal

from rose.common import ToolError, launch_detached

APT_LISTS = Path("/var/lib/apt/lists")
UPGRADE_CMDS = {
    "apt": "sudo apt update && sudo apt upgrade",
    "flatpak": "flatpak update",
}


def _out(cmd: list[str], timeout: float = 60) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace",
                           env={**os.environ, "LC_ALL": "C"})
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    return p.stdout


def _apt() -> dict:
    pkgs = []
    for ln in _out(["apt", "list", "--upgradable"]).splitlines():
        if "/" not in ln or ln.startswith("Listing"):
            continue
        name = ln.split("/", 1)[0]
        parts = ln.split()
        new = parts[1] if len(parts) > 1 else ""
        old = ln.rsplit("from: ", 1)[1].rstrip("]") if "from: " in ln else ""
        pkgs.append({"name": name, "from": old, "to": new, "security": "-security" in ln})
    try:
        age_h = round((time.time() - APT_LISTS.stat().st_mtime) / 3600, 1)
    except OSError:
        age_h = None
    return {"count": len(pkgs), "security": sum(p["security"] for p in pkgs), "packages": pkgs[:60],
            "lists_age_hours": age_h,
            "note": "From the last `apt update`; upgrade runs a fresh update first."}


UV_TOOLS = Path.home() / ".local" / "share" / "uv" / "tools"


def _uv_local(name: str) -> bool:
    """Installed from a local folder/editable (like ROSE itself): an "upgrade" would swap in an unrelated
    PyPI package of the same name, so these are never upgraded."""
    try:
        receipt = (UV_TOOLS / name / "uv-receipt.toml").read_text()
    except OSError:
        return True
    return "directory =" in receipt or "editable =" in receipt or "path =" in receipt


def _uv() -> dict:
    if not shutil.which("uv"):
        return {"count": 0, "tools": [], "names": []}
    tools, names = [], []
    for ln in _out(["uv", "tool", "list", "--outdated"], timeout=120).splitlines():
        if ln.startswith("-") or not ln.strip() or ln.startswith(" "):
            continue
        name = ln.split()[0]
        if _uv_local(name):
            continue
        tools.append(ln.strip())
        names.append(name)
    return {"count": len(tools), "tools": tools, "names": names}


def _flatpak() -> dict | None:
    if not shutil.which("flatpak"):
        return None
    apps = [ln.split("\t")[0] for ln in _out(["flatpak", "remote-ls", "--updates", "--columns=application"],
                                             timeout=90).splitlines() if ln.strip()]
    return {"count": len(apps), "apps": apps[:40]}


def package_updates(action: Literal["list", "upgrade"] = "list", target: Literal["apt", "uv", "flatpak", "all"] = "apt",
                    confirm: bool = False) -> dict:
    """Pending updates: apt (security counted), outdated uv tools (not local ones like ROSE), flatpak.
    upgrade: opens a terminal for `target` where the user types their sudo password; needs confirm=true."""
    if action == "list":
        out = {"apt": _apt(), "uv": _uv()}
        fp = _flatpak()
        if fp is not None:
            out["flatpak"] = fp
        return out
    if not confirm:
        raise ToolError(f"Upgrading {target} needs confirm=true. Ask the user first.")
    targets = [t for t in ("apt", "uv", "flatpak") if target in (t, "all")]
    if "flatpak" in targets and not shutil.which("flatpak"):
        targets.remove("flatpak")
    cmds = {**UPGRADE_CMDS}
    if "uv" in targets:
        names = _uv()["names"]
        if names:
            cmds["uv"] = "uv tool upgrade " + " ".join(shlex.quote(n) for n in names)
        else:
            targets.remove("uv")
    if not targets:
        return {"started": [], "note": "Nothing to upgrade."}
    cmd = " ; ".join(cmds[t] for t in targets)
    script = f'printf "\\033]0;rose: updates\\007"; echo "$ {cmd}"; {cmd}; echo; echo "[done] press Enter to close"; read -r _'
    exe = shutil.which("ghostty") or shutil.which("gnome-terminal") or shutil.which("x-terminal-emulator")
    if not exe:
        raise ToolError("No terminal found.")
    sep = "--" if Path(exe).name == "gnome-terminal" else "-e"
    launch_detached([exe, sep, "bash", "-lc", script], cwd=str(Path.home()), name="updates")
    return {"started": targets, "command": cmd, "note": "Opened a terminal; type your password there if asked."}
