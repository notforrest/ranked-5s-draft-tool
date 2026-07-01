"""Oracle's Elixir raw pro-game row storage."""
from __future__ import annotations

import sqlite3


def upsert_rows(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Each dict must have keys matching oe_games_raw columns (gameid, playerid, participantid,
    side, position, playername, teamname, teamid, champion_id, champion_name_raw, ban1..ban5,
    result, league, year, split, playoffs, date, patch, gamelength, datacompleteness, source_file).
    Upsert keyed on (gameid, participantid) so re-importing overlapping files is safe."""
    cols = ["gameid", "playerid", "participantid", "side", "position", "playername", "teamname",
            "teamid", "champion_id", "champion_name_raw", "ban1", "ban2", "ban3", "ban4", "ban5",
            "result", "league", "year", "split", "playoffs", "date", "patch", "gamelength",
            "datacompleteness", "source_file"]
    placeholders = ",".join("?" * len(cols))
    col_list = ",".join(cols)
    update_list = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("gameid", "participantid"))
    sql = (
        f"INSERT INTO oe_games_raw ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT(gameid, participantid) DO UPDATE SET {update_list}"
    )
    conn.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])
    return len(rows)


def get_player_rows_complete(conn: sqlite3.Connection) -> list[dict]:
    """All individual-player rows (excludes team-summary rows, i.e. position == 'team') with
    datacompleteness == 'complete' -- the base dataset for pro synergy/matchup/role-eligibility jobs."""
    rows = conn.execute(
        "SELECT * FROM oe_games_raw WHERE datacompleteness = 'complete' AND position != 'team' "
        "AND champion_id IS NOT NULL"
    ).fetchall()
    return [dict(r) for r in rows]
