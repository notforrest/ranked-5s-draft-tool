"""Champion mastery reads/writes."""
from __future__ import annotations

import sqlite3


def replace_player_mastery(conn: sqlite3.Connection, player_id: int, entries: list[dict]) -> None:
    """entries: raw Champion-Mastery-V4 DTOs (camelCase keys championId/championLevel/championPoints/
    lastPlayTime/championPointsSinceLastLevel/championPointsUntilNextLevel). Wholesale overwrite --
    mastery has no incremental 'since' filter server-side, the DTO is already a full snapshot."""
    conn.execute("DELETE FROM champion_mastery WHERE player_id = ?", (player_id,))
    conn.executemany(
        """
        INSERT INTO champion_mastery (player_id, champion_id, champion_level, champion_points,
                                       last_play_time_ms, champion_points_since_last_lvl,
                                       champion_points_until_next_lvl, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        [
            (player_id, e["championId"], e["championLevel"], e["championPoints"],
             e.get("lastPlayTime"), e.get("championPointsSinceLastLevel"),
             e.get("championPointsUntilNextLevel"))
            for e in entries
        ],
    )


def get_mastery(conn: sqlite3.Connection, player_id: int, champion_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM champion_mastery WHERE player_id = ? AND champion_id = ?",
        (player_id, champion_id),
    ).fetchone()
    return dict(row) if row else None


def get_player_mastery_distribution(conn: sqlite3.Connection, player_id: int) -> list[dict]:
    """All mastery rows for a player, sorted by points descending -- used to scale a given
    champion's mastery against this player's OWN distribution (not a global scale)."""
    rows = conn.execute(
        "SELECT * FROM champion_mastery WHERE player_id = ? ORDER BY champion_points DESC",
        (player_id,),
    ).fetchall()
    return [dict(r) for r in rows]
