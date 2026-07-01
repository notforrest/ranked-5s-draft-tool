"""Personal match-history cache + per-player incremental refresh watermarks."""
from __future__ import annotations

import sqlite3


def match_exists(conn: sqlite3.Connection, match_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM matches WHERE match_id = ?", (match_id,)).fetchone()
    return row is not None


def insert_match(conn: sqlite3.Connection, *, match_id: str, platform_region: str,
                  game_creation_ms: int, game_duration_s: int | None, queue_id: int | None,
                  game_mode: str | None, patch: str | None, raw_json_path: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO matches (match_id, platform_region, game_creation_ms, game_duration_s,
                                        queue_id, game_mode, patch, raw_json_path, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (match_id, platform_region, game_creation_ms, game_duration_s, queue_id, game_mode, patch,
         raw_json_path),
    )


def insert_participant(conn: sqlite3.Connection, *, match_id: str, puuid: str,
                        player_id: int | None, champion_id: int, role: str | None, win: bool,
                        kills: int | None, deaths: int | None, assists: int | None,
                        team_id: int | None) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO match_participants (match_id, puuid, player_id, champion_id, role,
                                                    win, kills, deaths, assists, team_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (match_id, puuid, player_id, champion_id, role, int(win), kills, deaths, assists, team_id),
    )


def get_teammates_in_match(conn: sqlite3.Connection, match_id: str, team_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM match_participants WHERE match_id = ? AND team_id = ?",
        (match_id, team_id),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_roster_participants(conn: sqlite3.Connection, queue_ids: tuple[int, ...]) -> list[dict]:
    """All match_participants rows for roster players (player_id NOT NULL), restricted to the
    given queue ids, joined with match queue_id -- the base dataset for personal stats + roster
    synergy/matchup aggregate jobs."""
    placeholders = ",".join("?" * len(queue_ids))
    rows = conn.execute(
        f"""
        SELECT mp.* FROM match_participants mp
        JOIN matches m ON m.match_id = mp.match_id
        WHERE mp.player_id IS NOT NULL AND m.queue_id IN ({placeholders})
        """,
        queue_ids,
    ).fetchall()
    return [dict(r) for r in rows]


def get_refresh_state(conn: sqlite3.Connection, player_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM refresh_state WHERE player_id = ?", (player_id,)).fetchone()
    return dict(row) if row else None


def upsert_refresh_state(conn: sqlite3.Connection, player_id: int, *,
                          last_match_fetched_ms: int | None, status: str,
                          error_message: str | None = None) -> None:
    conn.execute(
        """
        INSERT INTO refresh_state (player_id, last_match_fetched_ms, last_refreshed_at,
                                    last_refresh_status, last_error_message)
        VALUES (?, ?, datetime('now'), ?, ?)
        ON CONFLICT(player_id) DO UPDATE SET
            last_match_fetched_ms = COALESCE(excluded.last_match_fetched_ms, refresh_state.last_match_fetched_ms),
            last_refreshed_at = datetime('now'),
            last_refresh_status = excluded.last_refresh_status,
            last_error_message = excluded.last_error_message
        """,
        (player_id, last_match_fetched_ms, status, error_message),
    )


def start_refresh_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute("INSERT INTO refresh_runs (status) VALUES ('running')")
    return cur.lastrowid


def finish_refresh_run(conn: sqlite3.Connection, run_id: int, *, status: str, summary_json: str) -> None:
    conn.execute(
        "UPDATE refresh_runs SET finished_at = datetime('now'), status = ?, summary_json = ? WHERE run_id = ?",
        (status, summary_json, run_id),
    )


def get_refresh_run(conn: sqlite3.Connection, run_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM refresh_runs WHERE run_id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def get_latest_refresh_run(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM refresh_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    return dict(row) if row else None
