"""Roster (players) + game session / lineup CRUD."""
from __future__ import annotations

import json
import sqlite3


def upsert_player(conn: sqlite3.Connection, *, display_name: str, riot_game_name: str,
                   riot_tag_line: str, platform_region: str, account_region: str,
                   preferred_roles: list[str] | None = None) -> int:
    cur = conn.execute(
        """
        INSERT INTO players (display_name, riot_game_name, riot_tag_line, platform_region,
                              account_region, preferred_roles, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(riot_game_name, riot_tag_line) DO UPDATE SET
            display_name=excluded.display_name, platform_region=excluded.platform_region,
            account_region=excluded.account_region, preferred_roles=excluded.preferred_roles,
            updated_at=datetime('now')
        """,
        (display_name, riot_game_name, riot_tag_line, platform_region, account_region,
         json.dumps(preferred_roles or [])),
    )
    if cur.lastrowid:
        return cur.lastrowid
    row = conn.execute(
        "SELECT player_id FROM players WHERE riot_game_name = ? AND riot_tag_line = ?",
        (riot_game_name, riot_tag_line),
    ).fetchone()
    return row["player_id"]


def set_puuid(conn: sqlite3.Connection, player_id: int, puuid: str) -> None:
    conn.execute("UPDATE players SET puuid = ?, updated_at = datetime('now') WHERE player_id = ?",
                 (puuid, player_id))


def update_riot_id(conn: sqlite3.Connection, player_id: int, *, riot_game_name: str,
                    riot_tag_line: str, platform_region: str, account_region: str,
                    preferred_roles: list[str]) -> None:
    """Updates an existing player's Riot ID (and dependent region/role fields) in place --
    used when roster import detects this is the same person under a corrected/renamed Riot ID,
    rather than a new player. Clears the cached puuid: it was resolved for the OLD Riot ID, and
    leaving it in place would keep silently pulling mastery/match data for the wrong (or a
    no-longer-existing) account on the next refresh."""
    conn.execute(
        """
        UPDATE players SET riot_game_name = ?, riot_tag_line = ?, platform_region = ?,
               account_region = ?, preferred_roles = ?, puuid = NULL, updated_at = datetime('now')
        WHERE player_id = ?
        """,
        (riot_game_name, riot_tag_line, platform_region, account_region,
         json.dumps(preferred_roles), player_id),
    )


def get_active_players(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM players WHERE is_active = 1 ORDER BY display_name").fetchall()
    return [_row_to_player(r) for r in rows]


def get_player(conn: sqlite3.Connection, player_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM players WHERE player_id = ?", (player_id,)).fetchone()
    return _row_to_player(row) if row else None


def _row_to_player(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["preferred_roles"] = json.loads(d["preferred_roles"]) if d["preferred_roles"] else []
    return d


def create_session(conn: sqlite3.Connection, *, our_side: str, lineup: list[tuple[int, str]],
                    label: str | None = None) -> int:
    """lineup: list of (player_id, assigned_role)."""
    cur = conn.execute(
        "INSERT INTO game_sessions (our_side, label) VALUES (?, ?)", (our_side, label)
    )
    session_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO session_lineup (session_id, player_id, assigned_role) VALUES (?, ?, ?)",
        [(session_id, pid, role) for pid, role in lineup],
    )
    return session_id


def get_session_lineup(conn: sqlite3.Connection, session_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT sl.player_id, sl.assigned_role, p.display_name, p.puuid
        FROM session_lineup sl JOIN players p ON p.player_id = sl.player_id
        WHERE sl.session_id = ?
        """,
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_session(conn: sqlite3.Connection, session_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM game_sessions WHERE session_id = ?", (session_id,)).fetchone()
    return dict(row) if row else None
