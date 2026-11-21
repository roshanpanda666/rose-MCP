"""Token-lean tool output: what ROSE sends back to the model.

Every tool result passes through `shape()` (outermost wrapper, after the guard), which:

* serializes dicts/lists as compact JSON (no indentation, no spaces after separators) instead of the
  SDK's indent=2 pretty-print;
* drops empty fields (None, "", [], {}) from dicts, recursively;
* trims trailing whitespace and runs of blank lines in text, except for tools whose text is payload
  that must stay exact (file contents, clipboard, command output);
* caps the text at `max_output_chars` (config; 0 = no cap) with a note saying how much was cut;
* keeps images/audio untouched (they don't count towards the cap);
* records outputs bigger than `large_output_chars` in ~/rose/logs/output-YYYY-MM-DD.jsonl, so
  `rose tokens` can show which tools are token-heavy.

Security labels added by the guard (`_untrusted_content_notice`, `_injection_warning`) are kept as-is.
"""

from __future__ import annotations

import functools
import json
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from rose.config import LOG_DIR, load_config

# Tools whose text output is payload: whitespace is meaningful, so it isn't normalized (only capped).
VERBATIM = {"read_file", "clipboard_read", "run_command", "bridge_read"}
_BLANKS = re.compile(r"\n{3,}")
_TRAIL = re.compile(r"[ \t]+\n")


def prune(value: Any) -> Any:
    """Drop None / empty-string / empty-container fields from dicts, recursively (lists keep their items)."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            v = prune(v)
            if v is None or v == "" or v == [] or v == {}:
                continue
            out[k] = v
        return out
    if isinstance(value, list):
        return [prune(v) for v in value]
    return value


def to_text(value: Any) -> str:
    """Compact JSON for structured values; strings pass through."""
    if isinstance(value, str):
        return value
    return json.dumps(prune(value), ensure_ascii=False, separators=(",", ":"), default=str)


def tidy(text: str) -> str:
    """Strip trailing spaces on lines, collapse 3+ newlines to 2, strip the ends."""
    return _BLANKS.sub("\n\n", _TRAIL.sub("\n", text)).strip()


def cap(text: str, limit: int) -> tuple[str, bool]:
    """Cut `text` to `limit` chars (0 = unlimited), saying how much was left out."""
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit] + f"\n…[cut: {len(text) - limit} more chars; ask for less or narrow the request]", True


def _is_media(x: Any) -> bool:
    return type(x).__name__ in ("Image", "Audio")


def _log_size(name: str, chars: int, truncated: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "tool": name, "chars": chars,
           "tokens_est": chars // 4, "truncated": truncated}
    with (LOG_DIR / f"output-{datetime.now():%Y-%m-%d}.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")


def shape_result(name: str, result: Any, limit: int, log_over: int) -> Any:
    """Turn one tool result into compact text (or [text, media...] when media is present)."""
    verbatim = name in VERBATIM
    if isinstance(result, list) and any(_is_media(x) for x in result):
        parts, budget, raw_chars, truncated = [], limit, 0, False
        for x in result:
            if _is_media(x):
                parts.append(x)
                continue
            t = to_text(x)
            t = t if verbatim else tidy(t)
            raw_chars += len(t)
            t, cut = cap(t, budget) if limit > 0 else (t, False)
            truncated |= cut
            budget = max(200, budget - len(t)) if limit > 0 else 0
            parts.append(t)
        if log_over and raw_chars > log_over:
            _log_size(name, raw_chars, truncated)
        return parts
    text = to_text(result)
    if not verbatim:
        text = tidy(text)
    raw = len(text)
    text, truncated = cap(text, limit)
    if log_over and raw > log_over:
        _log_size(name, raw, truncated)
    return text


def shaped(name: str, fn: Callable) -> Callable:
    """Wrap a tool so its result goes through `shape_result` with the current config limits."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        result = fn(*args, **kwargs)
        cfg = load_config()
        return shape_result(name, result, int(cfg.get("max_output_chars", 12000) or 0),
                            int(cfg.get("large_output_chars", 4000) or 0))

    if hasattr(fn, "__signature__"):
        wrapper.__signature__ = fn.__signature__
    return wrapper


def report(days: int = 7) -> list[dict]:
    """Per-tool output sizes from the large-output log: calls over the threshold, avg/max tokens, cuts."""
    stats: dict[str, dict] = defaultdict(lambda: {"calls": 0, "total": 0, "max": 0, "truncated": 0})
    today = datetime.now().date()
    for i in range(days):
        path = LOG_DIR / f"output-{today - timedelta(days=i):%Y-%m-%d}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            s = stats[e["tool"]]
            s["calls"] += 1
            s["total"] += e["tokens_est"]
            s["max"] = max(s["max"], e["tokens_est"])
            s["truncated"] += bool(e.get("truncated"))
    rows = [{"tool": t, "large_calls": s["calls"], "avg_tokens": s["total"] // s["calls"],
             "max_tokens": s["max"], "truncated": s["truncated"], "total_tokens": s["total"]}
            for t, s in stats.items()]
    return sorted(rows, key=lambda r: -r["total_tokens"])
