"""System status via psutil."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

POWER_SUPPLY = Path("/sys/class/power_supply")
MIN_SAMPLE = 0.5  # seconds of CPU sampling behind every percentage

# psutil measures CPU% between two reads of the same Process object. Keeping the objects between calls
# means the counters are already primed, so only the first call (or one within MIN_SAMPLE of the last)
# has to sleep. Percentages then cover the time since the previous call (at least MIN_SAMPLE).
_lock = threading.Lock()
_procs: dict[int, Any] = {}  # pid -> psutil.Process
_last_sample = 0.0


def _sysfs(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _battery() -> dict | None:
    import psutil

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


def _sample() -> tuple[float, list[dict]]:
    """(total CPU %, per-process rows) over at least MIN_SAMPLE seconds."""
    global _last_sample
    import psutil  # lazy: keeps ROSE's startup light

    with _lock:
        current, fresh = {}, set()
        for p in psutil.process_iter(["pid", "name", "memory_percent"]):
            proc = _procs.get(p.pid)
            if proc is None or not proc.is_running():
                proc = p
                try:
                    proc.cpu_percent(None)  # prime a newcomer
                except psutil.Error:
                    continue
                fresh.add(p.pid)
            proc.info = p.info  # fresh name / memory for this call
            current[p.pid] = proc
        _procs.clear()
        _procs.update(current)
        if _last_sample == 0.0:  # first call: nothing primed yet
            psutil.cpu_percent(None)
            wait = MIN_SAMPLE
        else:
            wait = MIN_SAMPLE - (time.monotonic() - _last_sample)
        if wait > 0:
            time.sleep(wait)
        cpu_total = psutil.cpu_percent(None)
        _last_sample = time.monotonic()
        rows = []
        now = time.time()
        for pid, proc in current.items():
            try:
                cpu = proc.cpu_percent(None)
                if pid in fresh and wait <= 0:
                    # primed just now, so its own sample is too short: use its average since it started
                    t = proc.cpu_times()
                    cpu = 100 * (t.user + t.system) / max(MIN_SAMPLE, now - proc.create_time())
                rows.append({"pid": proc.info["pid"], "name": proc.info["name"],
                             "cpu_percent": round(cpu, 1),
                             "memory_percent": round(proc.info["memory_percent"] or 0, 1)})
            except psutil.Error:
                continue
    return cpu_total, rows


def system_status(top_n: int = 5) -> dict:
    """CPU, memory, disk, battery, uptime and top processes."""
    import psutil

    top_n = max(1, min(top_n, 20))
    cpu_total, rows = _sample()
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
