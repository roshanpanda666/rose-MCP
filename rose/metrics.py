"""Tree-first metrics: how often ROSE observes through the accessibility tree / DOM vs screenshots.

Reads the per-day tool-call logs (~/rose/logs/YYYY-MM-DD.jsonl) and groups GUI calls into "tasks":
bursts of GUI calls by one caller with no gap longer than TASK_GAP. The screenshot rate should drop as
the tree-first path takes over.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from rose.common import KIND
from rose.config import LOG_DIR

TASK_GAP = timedelta(minutes=5)


def _entries(days: int):
    today = datetime.now().date()
    for i in range(days - 1, -1, -1):
        path = LOG_DIR / f"{today - timedelta(days=i):%Y-%m-%d}.jsonl"
        if not path.exists():
            continue
        with path.open() as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                kind = e.get("kind") or KIND.get(e.get("tool", ""))  # older entries have no "kind"
                if kind and e.get("ts"):
                    yield e, kind


def _counts() -> dict:
    return {"tree": 0, "screenshot": 0, "coords": 0, "keys": 0, "failed_readbacks": 0}


def observation_stats(days: int = 1, recent_tasks: int = 10) -> dict:
    """Tree-first metrics: counts of accessibility-tree/DOM calls vs screenshots vs coordinate clicks over
    the last `days` days, per caller, plus the most recent GUI "tasks" (bursts of GUI calls). Use it to
    check that screenshots are only the fallback."""
    days = max(1, min(days, 30))
    totals, per_caller, tasks, open_task = _counts(), {}, [], {}
    for e, kind in _entries(days):
        who = e.get("caller", "?")
        ts = datetime.fromisoformat(e["ts"])
        failed = "READ-BACK FAILED" in str(e.get("result", ""))
        for c in (totals, per_caller.setdefault(who, _counts())):
            c[kind] += 1
            c["failed_readbacks"] += failed
        t = open_task.get(who)
        if t is None or ts - t["_last"] > TASK_GAP:
            t = {"caller": who, "start": e["ts"], **_counts(), "_last": ts}
            tasks.append(t)
            open_task[who] = t
        t[kind] += 1
        t["failed_readbacks"] += failed
        t["_last"] = ts

    def rate(c):
        obs = c["tree"] + c["screenshot"]
        return f"{c['screenshot'] / obs:.0%}" if obs else "n/a"

    for c in [totals, *per_caller.values()]:
        c["screenshot_rate"] = rate(c)
    recent = []
    for t in tasks[-max(0, min(recent_tasks, 50)):]:
        t = {k: v for k, v in t.items() if k != "_last"}
        t["zero_screenshots"] = t["screenshot"] == 0
        recent.append(t)
    return {"days": days, "totals": totals, "per_caller": per_caller, "tasks": len(tasks),
            "tasks_without_screenshots": sum(1 for t in tasks if t["screenshot"] == 0),
            "recent_tasks": recent}
