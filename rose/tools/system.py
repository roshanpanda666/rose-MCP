"""System status via psutil."""

from __future__ import annotations

import time
from pathlib import Path

import psutil

POWER_SUPPLY = Path("/sys/class/power_supply")


def _sysfs(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _battery() -> dict | None:
    battery = psutil.sensors_battery()
    if battery is None:
        return None
    plugged = battery.power_plugged
    status = None
    if POWER_SUPPLY.is_dir():
        for dev in POWER_SUPPLY.iterdir():
            kind = _sysfs(dev / "type")
            if kind == "Mains" and plugged is None:
                plugged = _sysfs(dev / "online") == "1"
            elif kind == "Battery" and status is None:
                status = _sysfs(dev / "status")
    secs = battery.secsleft
    return {
        "percent": round(battery.percent),
        "plugged_in": plugged,
        "status": status,  # e.g. Charging, Discharging, Full, Not charging
        "minutes_left": None
        if plugged or secs in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN)
        else secs // 60,
    }


def _gb(n: int) -> float:
    return round(n / 1024**3, 1)


def system_status(top_n: int = 5) -> dict:
    """CPU, memory, disk, battery and the top processes by CPU and memory."""
    top_n = max(1, min(top_n, 20))

    # Prime per-process CPU counters, wait briefly, then read real percentages.
    procs = list(psutil.process_iter(["pid", "name", "username", "memory_percent"]))
    for p in procs:
        try:
            p.cpu_percent(None)
        except psutil.Error:
            pass
    cpu_total = psutil.cpu_percent(interval=0.5)
    rows = []
    for p in procs:
        try:
            rows.append(
                {
                    "pid": p.info["pid"],
                    "name": p.info["name"],
                    "cpu_percent": round(p.cpu_percent(None), 1),
                    "memory_percent": round(p.info["memory_percent"] or 0, 1),
                }
            )
        except psutil.Error:
            continue

    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    load1, load5, load15 = psutil.getloadavg()

    return {
        "cpu": {
            "percent": cpu_total,
            "cores": psutil.cpu_count(),
            "load_avg": [round(load1, 2), round(load5, 2), round(load15, 2)],
        },
        "memory": {
            "percent": mem.percent,
            "used_gb": _gb(mem.total - mem.available),
            "total_gb": _gb(mem.total),
        },
        "disk_root": {
            "percent": disk.percent,
            "free_gb": _gb(disk.free),
            "total_gb": _gb(disk.total),
        },
        "battery": _battery(),
        "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
        "top_by_cpu": sorted(rows, key=lambda r: r["cpu_percent"], reverse=True)[:top_n],
        "top_by_memory": sorted(rows, key=lambda r: r["memory_percent"], reverse=True)[:top_n],
    }
