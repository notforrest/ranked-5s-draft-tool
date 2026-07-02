"""Live rank/LP per roster player, sourced from the OP.GG MCP server (see
staticdata/opgg_mcp_client.py) -- no other data source in this codebase has rank/LP at all."""
from __future__ import annotations

import sqlite3


def replace_player_rank(conn: sqlite3.Connection, player_id: int, entries: list[dict]) -> None:
    """Wholesale overwrite for one player's rank rows, mirroring mastery_repo's
    replace_player_mastery -- rank standings are a point-in-time snapshot that should always be
    refetched, not incrementally merged. `entries`: list of
    {"queue_type", "tier", "division", "lp", "wins", "losses"}."""
    conn.execute("DELETE FROM summoner_rank WHERE player_id = ?", (player_id,))
    conn.executemany(
        """
        INSERT INTO summoner_rank (player_id, queue_type, tier, division, lp, wins, losses, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        [
            (player_id, e["queue_type"], e.get("tier"), e.get("division"),
             e.get("lp"), e.get("wins"), e.get("losses"))
            for e in entries
        ],
    )


def get_player_rank(conn: sqlite3.Connection, player_id: int, queue_type: str = "SOLORANKED") -> dict | None:
    row = conn.execute(
        "SELECT * FROM summoner_rank WHERE player_id = ? AND queue_type = ?", (player_id, queue_type)
    ).fetchone()
    return dict(row) if row else None


def get_all_ranks_for_player(conn: sqlite3.Connection, player_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM summoner_rank WHERE player_id = ? ORDER BY queue_type", (player_id,)
    ).fetchall()
    return [dict(r) for r in rows]
