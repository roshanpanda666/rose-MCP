"""Weekly failure report: failed tool calls and Rose's "I can't" replies, so the gaps get fixed.

Sources (last N days):
* ROSE call log ~/rose/logs/YYYY-MM-DD.jsonl: every tool call with ok=false (any client).
* Phone app (rose-phone-app/backend/logs, found next to companion_env_file):
  tool-calls.jsonl (ok=false, includes the app's local tools) and replies.jsonl ("I can't" drafts the
  give-up guard dropped, and "can't" replies the user actually saw).
Saved as markdown in ~/rose/reports/failures-YYYY-MM-DD.md. rose-watch writes it weekly
(failure_report_day / failure_report_time) and pushes a summary; `rose failures` prints it any time.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from rose.config import LOG_DIR, ROSE_HOME, load_config

REPORTS = ROSE_HOME / "reports"
# Same idea as the phone app's GIVE_UP pattern (straight and curly apostrophes).
CANT = re.compile(r"\b(?:can['’]?t|cannot|can not|unable to|not able to|no (?:tool|way) to|"
                  r"(?:don['’]?t|do not) have (?:a |the |any )?(?:tool|access|way|ability|permission))", re.I)


def _phone_logs() -> Path | None:
    env = load_config().get("companion_env_file") or ""
    if not env:
        return None
    d = Path(env).expanduser().parent / "logs"
    return d if d.is_dir() else None


def _rose_tool_names() -> set[str]:
    try:
        from rose.server import _tool_table
        return {fn.__name__ for fn, _ in _tool_table()}
    except Exception:
        return set()


def _jsonl(path: Path):
    try:
        with path.open(errors="replace") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _in_window(ts: str, since: datetime) -> bool:
    try:
        return datetime.fromisoformat(str(ts)[:19]) >= since
    except ValueError:
        return False


def _error_kind(err: str) -> str:
    """Group similar errors: drop numbers, paths, quoted values."""
    e = re.sub(r"'[^']*'|\"[^\"]*\"", "'…'", str(err))
    e = re.sub(r"(/[\w.~-]+)+", "<path>", e)
    e = re.sub(r"\d+", "N", e)
    return e[:140]


def collect(days: int = 7, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    since = now - timedelta(days=days)
    failed: list[dict] = []
    calls = 0
    for i in range(days + 1):
        path = LOG_DIR / f"{(now - timedelta(days=i)):%Y-%m-%d}.jsonl"
        for e in _jsonl(path):
            if "tool" not in e or "ok" not in e or not _in_window(e.get("ts", ""), since):
                continue
            calls += 1
            if not e["ok"]:
                failed.append({"source": "rose", "ts": e["ts"], "tool": e["tool"], "caller": e.get("caller", "?"),
                               "error": str(e.get("error", ""))[:300], "args": e.get("args", {})})
    cant: list[dict] = []
    phone_calls = 0
    logs = _phone_logs()
    if logs:
        rose_tools = _rose_tool_names()
        for e in _jsonl(logs / "tool-calls.jsonl"):
            if not _in_window(e.get("ts", ""), since):
                continue
            phone_calls += 1
            # ROSE tools the app called are already in ROSE's own log (caller "Phone app"): count only the
            # app's local tools here, or every failure would be counted twice.
            if e.get("ok") is False and e.get("tool") not in rose_tools:
                failed.append({"source": "phone app", "ts": e["ts"], "tool": e.get("tool", "?"),
                               "caller": "Phone app", "error": str(e.get("error", "") or "failed (see app log)")[:300],
                               "args": e.get("args", "")})
        for e in _jsonl(logs / "replies.jsonl"):
            if _in_window(e.get("ts", ""), since) and CANT.search(str(e.get("text", ""))):
                cant.append({"ts": e["ts"], "kind": e.get("kind", "reply"), "user": str(e.get("user", ""))[:200],
                             "text": str(e.get("text", ""))[:300], "tools_used": e.get("tools_used", [])})
    return {"since": since.isoformat(timespec="minutes"), "until": now.isoformat(timespec="minutes"), "days": days,
            "rose_calls": calls, "phone_calls": phone_calls, "failed": failed, "cant": cant}


def summarize(data: dict) -> dict:
    by_tool = Counter(f["tool"] for f in data["failed"])
    kinds: dict[str, Counter] = defaultdict(Counter)
    example: dict[tuple, dict] = {}
    for f in data["failed"]:
        k = _error_kind(f["error"])
        kinds[f["tool"]][k] += 1
        example.setdefault((f["tool"], k), f)
    top = [{"tool": t, "failures": n,
            "errors": [{"error": k, "count": c, "example": example[(t, k)]} for k, c in kinds[t].most_common(3)]}
           for t, n in by_tool.most_common(25)]
    shown = [c for c in data["cant"] if c["kind"] != "give_up_dropped"]
    return {"failed_calls": len(data["failed"]), "tools_failing": len(by_tool), "cant_replies": len(shown),
            "cant_dropped": len(data["cant"]) - len(shown), "top": top}


def render(data: dict, s: dict | None = None) -> str:
    s = s or summarize(data)
    total = data["rose_calls"] or 1
    lines = [f"# ROSE failure report: {data['since'][:10]} to {data['until'][:10]}", "",
             f"- Tool calls logged by ROSE: {data['rose_calls']} ({s['failed_calls']} failed, "
             f"{100 * s['failed_calls'] / total:.1f}%) across {s['tools_failing']} tools",
             f"- Phone app tool calls: {data['phone_calls']}",
             f"- \"I can't\" replies the user saw: {s['cant_replies']}; give-up drafts caught and retried: "
             f"{s['cant_dropped']}", ""]
    if s["top"]:
        lines += ["## Failing tools", "", "| Tool | Failures | Most common error |", "|---|---:|---|"]
        for t in s["top"]:
            err = t["errors"][0]["error"].replace("|", "\\|") if t["errors"] else ""
            lines.append(f"| {t['tool']} | {t['failures']} | {err} |")
        lines += ["", "## Examples", ""]
        for t in s["top"][:10]:
            for e in t["errors"]:
                ex = e["example"]
                lines.append(f"- **{t['tool']}** ×{e['count']} ({ex['caller']}, {ex['ts']}): `{ex['error'][:200]}`"
                             f" args: `{str(ex['args'])[:160]}`")
        lines.append("")
    if data["cant"]:
        lines += ["## \"I can't\" replies", ""]
        for c in data["cant"][-30:]:
            tag = "caught+retried" if c["kind"] == "give_up_dropped" else "shown"
            lines.append(f"- {c['ts']} [{tag}] user: \"{c['user'][:120]}\" -> Rose: \"{c['text'][:200]}\"")
        lines.append("")
    if not s["top"] and not data["cant"]:
        lines.append("No failed tool calls and no \"I can't\" replies. 🎉")
    return "\n".join(lines) + "\n"


def report(days: int = 7, save: bool = False) -> dict:
    data = collect(days)
    s = summarize(data)
    md = render(data, s)
    out = {**{k: s[k] for k in ("failed_calls", "tools_failing", "cant_replies", "cant_dropped")},
           "rose_calls": data["rose_calls"], "phone_calls": data["phone_calls"],
           "top": [{"tool": t["tool"], "failures": t["failures"], "error": t["errors"][0]["error"]}
                   for t in s["top"][:10]], "markdown": md}
    if save:
        REPORTS.mkdir(parents=True, exist_ok=True)
        path = REPORTS / f"failures-{datetime.now():%Y-%m-%d}.md"
        path.write_text(md)
        out["saved"] = str(path)
    return out


def failure_report(days: int = 7) -> dict:
    """Failed tool calls (all clients + phone app) and Rose's "I can't" replies over the last N days (1-60):
    counts, top failing tools with their most common error, and a markdown report."""
    return report(max(1, min(int(days), 60)))


def weekly_due(state: dict, now: datetime | None = None) -> bool:
    """True once per week at failure_report_day / failure_report_time (records it in state)."""
    cfg = load_config()
    day = str(cfg.get("failure_report_day", "mon") or "").lower()[:3]
    if not day:
        return False
    now = now or datetime.now()
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    if day not in days or now.weekday() != days.index(day):
        return False
    try:
        h, m = map(int, str(cfg.get("failure_report_time", "09:00")).split(":"))
    except ValueError:
        h, m = 9, 0
    if (now.hour, now.minute) < (h, m):
        return False
    if state.get("failure_report_last") == now.strftime("%Y-%m-%d"):
        return False
    state["failure_report_last"] = now.strftime("%Y-%m-%d")
    return True


def send_weekly() -> dict:
    from rose.push import push
    r = report(7, save=True)
    tops = ", ".join(f"{t['tool']} ×{t['failures']}" for t in r["top"][:4]) or "none"
    push("📋 ROSE weekly failure report",
         f"{r['failed_calls']} failed calls ({tops}); {r['cant_replies']} \"I can't\" replies. "
         f"Full report: {r.get('saved', '')}", tags="clipboard")
    try:
        from rose.tools.bridge import post_as
        post_as("ROSE", r["markdown"][:6000], "all", email=False)
    except Exception:
        pass
    return r
