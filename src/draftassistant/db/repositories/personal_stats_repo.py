"""Derived personal per-champion aggregate stats -- rebuilt wholesale by aggregate/personal_stats.py."""
from __future__ import annotations

import sqlite3


def replace_all(conn: sqlite3.Connection, rows: list[tuple[int, int, str, int, int, int | None]]) -> None:
    """rows: list of (player_id, champion_id, role, games, wins, last_played_ms)."""
    conn.execute("DELETE FROM personal_champion_stats")
    conn.executemany(
        """
        INSERT INTO personal_champion_stats (player_id, champion_id, role, games, wins,
                                               last_played_ms, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        rows,
    )


def get_personal_stats(conn: sqlite3.Connection, player_id: int, champion_id: int) -> dict | None:
    """Sums across all roles the player has played this champion in -- callers wanting
    role-specific stats should filter separately; most callers just want overall games/wins."""
    row = conn.execute(
        """
        SELECT player_id, champion_id, SUM(games) AS games, SUM(wins) AS wins,
               MAX(last_played_ms) AS last_played_ms
        FROM personal_champion_stats WHERE player_id = ? AND champion_id = ?
        GROUP BY player_id, champion_id
        """,
        (player_id, champion_id),
    ).fetchone()
    if row is None or row["games"] is None:
        return None
    return dict(row)
