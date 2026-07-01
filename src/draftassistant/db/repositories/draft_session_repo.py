"""Server-side persistence of live draft state (belt-and-suspenders alongside browser
localStorage -- a server restart mid-draft shouldn't lose state either)."""
from __future__ import annotations

import json
import sqlite3


def create_draft_session(conn: sqlite3.Connection, *, session_id: int | None, our_side: str) -> int:
    cur = conn.execute(
        "INSERT INTO draft_sessions (session_id, our_side, current_slot, entries_json) VALUES (?, ?, 0, '[]')",
        (session_id, our_side),
    )
    return cur.lastrowid


def save_draft_state(conn: sqlite3.Connection, draft_session_id: int, *, current_slot: int,
                      entries: list[dict | None]) -> None:
    conn.execute(
        "UPDATE draft_sessions SET current_slot = ?, entries_json = ?, updated_at = datetime('now') "
        "WHERE draft_session_id = ?",
        (current_slot, json.dumps(entries), draft_session_id),
    )


def get_draft_state(conn: sqlite3.Connection, draft_session_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM draft_sessions WHERE draft_session_id = ?", (draft_session_id,)
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["entries"] = json.loads(d["entries_json"])
    return d


def get_latest_draft_session(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM draft_sessions ORDER BY draft_session_id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    d = dict(row)
    d["entries"] = json.loads(d["entries_json"])
    return d
