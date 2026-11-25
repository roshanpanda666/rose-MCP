"""Docker: list containers, read their logs, start/stop/restart them (stop/restart need confirm=true)."""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Literal

from rose.common import ToolError

MAX_LOG_LINES = 400


def _docker(*args: str, timeout: float = 30) -> str:
    if not shutil.which("docker"):
        raise ToolError("Docker isn't installed.")
    try:
        p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired:
        raise ToolError(f"docker {args[0]} timed out after {timeout:.0f}s.") from None
    if p.returncode != 0:
        err = (p.stderr or p.stdout).strip()
        if "docker.sock" in err or ("daemon" in err.lower() and "running" in err.lower()):
            raise ToolError("Docker isn't running. Start Docker Desktop (open_app('Docker Desktop')) and retry.")
        raise ToolError(f"docker {args[0]} failed: {err[-400:] or 'no output'}")
    return p.stdout


def _containers(all_: bool) -> list[dict]:
    out = _docker("ps", "--format", "{{json .}}", *(["-a"] if all_ else []))
    rows = []
    for ln in out.splitlines():
        try:
            c = json.loads(ln)
        except ValueError:
            continue
        rows.append({"name": c.get("Names", ""), "image": c.get("Image", ""), "state": c.get("State", ""),
                     "status": c.get("Status", ""), "ports": c.get("Ports", "")[:120],
                     "project": (c.get("Labels", "") or "").partition("com.docker.compose.project=")[2]
                     .split(",")[0]})
    return rows


def _pick(container: str) -> str:
    q = container.strip().lower()
    if not q:
        raise ToolError("Name the container (docker(action='ps', all=true) lists them).")
    rows = _containers(True)
    exact = [r for r in rows if r["name"].lower() == q]
    hits = exact or [r for r in rows if q in r["name"].lower() or q in r["image"].lower()]
    if not hits:
        raise ToolError(f"No container like '{container}'. Containers: "
                        + (", ".join(r["name"] for r in rows) or "none"))
    if len(hits) > 1:
        raise ToolError(f"Several containers match '{container}': " + ", ".join(r["name"] for r in hits)
                        + ". Say which.")
    return hits[0]["name"]


def docker(action: Literal["ps", "logs", "start", "stop", "restart"] = "ps", container: str = "",
           all: bool = False, lines: int = 80, confirm: bool = False) -> dict | list[dict] | str:
    """Docker containers. ps: running ones (all=true: stopped too). logs: last `lines` of a container's log.
    start: start a stopped container. stop/restart: need confirm=true (ask first). Container = name or part of it."""
    if action == "ps":
        rows = _containers(all)
        return rows or [{"none": "No containers" + ("" if all else " running (all=true shows stopped ones)") + "."}]
    name = _pick(container)
    if action == "logs":
        n = max(1, min(lines, MAX_LOG_LINES))
        p = subprocess.run(["docker", "logs", "--tail", str(n), "--timestamps", name], capture_output=True,
                           text=True, timeout=30, errors="replace")
        text = (p.stdout + p.stderr).strip()
        return {"container": name, "lines": n, "log": text[-20000:] or "(empty)"}
    if action in ("stop", "restart") and not confirm:
        raise ToolError(f"{action.capitalize()} container {name} needs confirm=true. Ask the user first.")
    _docker(action, name, timeout=90)
    return f"{action.capitalize()}ed {name}." if action != "stop" else f"Stopped {name}."
