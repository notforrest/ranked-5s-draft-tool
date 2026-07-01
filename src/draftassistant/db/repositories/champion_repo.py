"""Static champion data + empirically-derived role eligibility.

Shared contract module: both the data/backend layer (writes, via ingest/ddragon_sync.py and
aggregate jobs) and the draft engine (reads, via scoring) depend on these exact signatures.
"""
from __future__ import annotations

import json
import sqlite3


def upsert_champion(conn: sqlite3.Connection, *, champion_id: int, champion_key: str,
                     name: str, tags: list[str], icon_path: str, ddragon_version: str) -> None:
    conn.execute(
        """
        INSERT INTO champions (champion_id, champion_key, name, tags, icon_path, ddragon_version, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(champion_id) DO UPDATE SET
            champion_key=excluded.champion_key, name=excluded.name, tags=excluded.tags,
            icon_path=excluded.icon_path, ddragon_version=excluded.ddragon_version,
            updated_at=datetime('now')
        """,
        (champion_id, champion_key, name, json.dumps(tags), icon_path, ddragon_version),
    )


def get_all_champions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM champions ORDER BY name").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = json.loads(d["tags"]) if d["tags"] else []
        out.append(d)
    return out


def get_champion_by_id(conn: sqlite3.Connection, champion_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM champions WHERE champion_id = ?", (champion_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["tags"] = json.loads(d["tags"]) if d["tags"] else []
    return d


def get_champion_by_name(conn: sqlite3.Connection, name: str) -> dict | None:
    """Case-insensitive exact match on display name -- used by curated tier-list / OE import
    to resolve human-typed champion names to champion_id."""
    row = conn.execute("SELECT * FROM champions WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["tags"] = json.loads(d["tags"]) if d["tags"] else []
    return d


def replace_role_eligibility(conn: sqlite3.Connection, source: str, rows: list[tuple[int, str, int]]) -> None:
    """rows: list of (champion_id, role, games_observed). Wholesale-replaces all rows for this
    `source` ('personal' or 'pro') -- called by aggregate jobs which always recompute from scratch."""
    conn.execute("DELETE FROM champion_role_eligibility WHERE source = ?", (source,))
    conn.executemany(
        "INSERT INTO champion_role_eligibility (champion_id, role, source, games_observed) VALUES (?, ?, ?, ?)",
        [(cid, role, source, games) for cid, role, games in rows],
    )


def get_role_eligibility(conn: sqlite3.Connection, champion_id: int) -> dict[str, dict]:
    """Returns {role: {"source": "pro"|"personal", "games_observed": int}} -- one entry per role
    this champion has ANY observed eligibility for, preferring the source with more games observed
    when both 'pro' and 'personal' have rows for the same role."""
    rows = conn.execute(
        "SELECT role, source, games_observed FROM champion_role_eligibility WHERE champion_id = ?",
        (champion_id,),
    ).fetchall()
    best: dict[str, dict] = {}
    for r in rows:
        role = r["role"]
        if role not in best or r["games_observed"] > best[role]["games_observed"]:
            best[role] = {"source": r["source"], "games_observed": r["games_observed"]}
    return best
