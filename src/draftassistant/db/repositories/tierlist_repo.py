"""Curated global tier-list reads/writes."""
from __future__ import annotations

import sqlite3


def upsert_tier_entry(conn: sqlite3.Connection, *, champion_id: int, role: str, patch: str,
                       win_rate: float | None, pick_rate: float | None, ban_rate: float | None,
                       tier: str | None, sample_size: int | None, source_note: str | None) -> None:
    conn.execute(
        """
        INSERT INTO global_tier_list (champion_id, role, patch, win_rate, pick_rate, ban_rate,
                                       tier, sample_size, source_note, imported_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(champion_id, role, patch) DO UPDATE SET
            win_rate=excluded.win_rate, pick_rate=excluded.pick_rate, ban_rate=excluded.ban_rate,
            tier=excluded.tier, sample_size=excluded.sample_size, source_note=excluded.source_note,
            imported_at=datetime('now')
        """,
        (champion_id, role, patch, win_rate, pick_rate, ban_rate, tier, sample_size, source_note),
    )


def get_tier_entry(conn: sqlite3.Connection, champion_id: int, role: str, patch: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM global_tier_list WHERE champion_id = ? AND role = ? AND patch = ?",
        (champion_id, role, patch),
    ).fetchone()
    return dict(row) if row else None


def get_tier_list_for_patch(conn: sqlite3.Connection, patch: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM global_tier_list WHERE patch = ?", (patch,)).fetchall()
    return [dict(r) for r in rows]


def get_latest_patch(conn: sqlite3.Connection) -> str | None:
    """Highest imported patch string available -- used as the default when the caller doesn't
    pin a specific patch. Patch strings sort correctly as text for same-season values like '14.13'."""
    row = conn.execute("SELECT patch FROM global_tier_list ORDER BY patch DESC LIMIT 1").fetchone()
    return row["patch"] if row else None
