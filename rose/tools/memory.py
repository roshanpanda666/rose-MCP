"""Memory: small facts Rose should remember across chats (preferences, facts, projects, tasks).

Stored locally in ~/rose/memory.db (SQLite). Search is full-text with BM25 ranking (FTS5); if this SQLite has
no FTS5, a plain LIKE search is used instead. Near-identical saves update the existing memory instead of adding
a duplicate. Stored text is user data: it is only ever returned as data, never run or interpreted.
"""

from __future__ import annotations

import contextlib
import difflib
import re
import sqlite3
import threading
import time
from datetime import datetime
from typing import Literal

from rose.common import ToolError
from rose.config import ROSE_HOME

DB_PATH = ROSE_HOME / "memory.db"
MAX_TEXT = 1000  # characters per memory
MAX_TAGS = 200
MAX_LIMIT = 50
SIMILAR = 0.92  # difflib ratio at/above which two memories count as the same
KINDS = ("preference", "fact", "project", "task")
Kind = Literal["preference", "fact", "project", "task"]

_lock = threading.Lock()


def _norm(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


def _clean(text: str, limit: int) -> str:
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text or "")
    return re.sub(r"[ \t]+", " ", text).strip()[:limit]


def _clean_tags(tags: str) -> str:
    parts = [t.strip().lower() for t in re.split(r"[,\s]+", tags or "") if t.strip()]
    return ",".join(dict.fromkeys(parts))[:MAX_TAGS]


def _connect() -> tuple[sqlite3.Connection, bool]:
    """Open (and create) the database. Returns (connection, has_fts)."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'fact', norm TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)""")
    con.execute("CREATE INDEX IF NOT EXISTS memories_norm ON memories(norm)")
    has_fts = True
    try:
        con.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            text, tags, content='memories', content_rowid='id')""")
        con.executescript("""
        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
          INSERT INTO memories_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags); END;
        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
          INSERT INTO memories_fts(memories_fts, rowid, text, tags) VALUES ('delete', old.id, old.text, old.tags); END;
        CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
          INSERT INTO memories_fts(memories_fts, rowid, text, tags) VALUES ('delete', old.id, old.text, old.tags);
          INSERT INTO memories_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags); END;
        """)
    except sqlite3.OperationalError:  # SQLite built without FTS5
        has_fts = False
    con.commit()
    return con, has_fts


@contextlib.contextmanager
def _db():
    with _lock:
        con, has_fts = _connect()
        try:
            yield con, has_fts
            con.commit()
        finally:
            con.close()


def _row(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "kind": r["kind"], "text": r["text"], "tags": r["tags"],
            "updated": datetime.fromtimestamp(r["updated"]).strftime("%Y-%m-%d %H:%M")}


def _fts_query(query: str) -> str:
    """User words as quoted FTS5 prefix terms, OR-ed (BM25 ranks the best matches first). No FTS syntax passes."""
    words = re.findall(r"\w+", query.lower())[:12]
    return " OR ".join(f'"{w}"*' for w in words)


def _search(con: sqlite3.Connection, has_fts: bool, query: str, limit: int, kind: str = "") -> list[sqlite3.Row]:
    words = re.findall(r"\w+", query.lower())[:12]
    if not words:
        return []
    kind_sql, kind_args = (" AND m.kind = ?", [kind]) if kind else ("", [])
    if has_fts:
        return con.execute(
            "SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid "
            f"WHERE memories_fts MATCH ?{kind_sql} ORDER BY bm25(memories_fts, 1.0, 0.5) LIMIT ?",
            [_fts_query(query), *kind_args, limit]).fetchall()
    # Fallback: rows containing any word, ranked by how many words they contain
    like = " OR ".join(["(lower(m.text) LIKE ? ESCAPE '\\' OR lower(m.tags) LIKE ? ESCAPE '\\')"] * len(words))
    args: list = []
    for w in words:
        pat = "%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        args += [pat, pat]
    rows = con.execute(f"SELECT m.* FROM memories m WHERE ({like}){kind_sql} ORDER BY m.updated DESC LIMIT 200",
                       [*args, *kind_args]).fetchall()
    def hits(r):
        hay = (r["text"] + " " + r["tags"]).lower()
        return sum(w in hay for w in words)
    return sorted(rows, key=hits, reverse=True)[:limit]


def memory_save(text: str, tags: str = "", kind: Kind = "fact") -> dict:
    """Remember something about the user for later chats (a preference, fact, project or task). Near-identical
    memories are updated, not duplicated. tags: comma-separated words."""
    text = _clean(text, MAX_TEXT)
    if not _norm(text):
        raise ToolError("Nothing to remember: text is empty.")
    if kind not in KINDS:
        raise ToolError(f"kind must be one of: {', '.join(KINDS)}")
    tags = _clean_tags(tags)
    norm = _norm(text)
    now = time.time()
    with _db() as (con, has_fts):
        same = con.execute("SELECT * FROM memories WHERE norm = ? LIMIT 1", (norm,)).fetchone()
        if same is None:
            for r in _search(con, has_fts, text, 20):
                if difflib.SequenceMatcher(None, r["norm"], norm).ratio() >= SIMILAR:
                    same = r
                    break
        if same is not None:
            merged = _clean_tags(",".join(filter(None, [same["tags"], tags])))
            con.execute("UPDATE memories SET text = ?, tags = ?, kind = ?, norm = ?, updated = ? WHERE id = ?",
                        (text, merged, kind, norm, now, same["id"]))
            row = con.execute("SELECT * FROM memories WHERE id = ?", (same["id"],)).fetchone()
            return {"saved": "updated existing", **_row(row)}
        cur = con.execute("INSERT INTO memories(text, tags, kind, norm, created, updated) VALUES (?,?,?,?,?,?)",
                          (text, tags, kind, norm, now, now))
        row = con.execute("SELECT * FROM memories WHERE id = ?", (cur.lastrowid,)).fetchone()
        return {"saved": "new", **_row(row)}


def memory_search(query: str, limit: int = 8) -> list[dict]:
    """Search Rose's saved memories about the user (best matches first)."""
    limit = max(1, min(int(limit), MAX_LIMIT))
    with _db() as (con, has_fts):
        return [_row(r) for r in _search(con, has_fts, query, limit)]


def memory_list(kind: str = "", limit: int = 20) -> list[dict]:
    """Most recent saved memories, optionally only one kind (preference, fact, project, task)."""
    if kind and kind not in KINDS:
        raise ToolError(f"kind must be empty or one of: {', '.join(KINDS)}")
    limit = max(1, min(int(limit), MAX_LIMIT))
    with _db() as (con, _):
        if kind:
            rows = con.execute("SELECT * FROM memories WHERE kind = ? ORDER BY updated DESC LIMIT ?", (kind, limit))
        else:
            rows = con.execute("SELECT * FROM memories ORDER BY updated DESC LIMIT ?", (limit,))
        return [_row(r) for r in rows.fetchall()]


def memory_forget(memory_id: int, confirm: bool = False) -> str:
    """Delete one saved memory by id (from memory_search/memory_list). Needs confirm=true."""
    with _db() as (con, _):
        row = con.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
        if row is None:
            raise ToolError(f"No memory with id {memory_id}.")
        if not confirm:
            raise ToolError(f"This deletes memory {memory_id} ({row['kind']}): {row['text'][:120]!r}. "
                            "To delete it, call again with confirm=true.")
        con.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))
    return f"Forgot memory {memory_id}."


CORE_TAG = "core"
KIND_ORDER = {"fact": 0, "preference": 1, "project": 2, "task": 3}


def core_context(max_chars: int = 3500) -> str:
    """The memories tagged 'core' (who the user is, where projects live, how they work) as short bullet lines,
    for clients to put in every chat. '' when there are none or the database can't be read."""
    try:
        with _db() as (con, _):
            rows = con.execute("SELECT * FROM memories WHERE ',' || tags || ',' LIKE ? ORDER BY updated",
                               (f"%,{CORE_TAG},%",)).fetchall()
    except Exception:
        return ""
    lines, used = [], 0
    for r in sorted(rows, key=lambda r: KIND_ORDER.get(r["kind"], 9)):
        line = "- " + r["text"]
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)
