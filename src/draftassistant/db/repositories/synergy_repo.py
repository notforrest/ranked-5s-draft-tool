"""Pairwise ally-synergy (same team) and matchup (opposing team) aggregates.

Synergy tables are stored with canonical ordering champion_id_a < champion_id_b (order doesn't
matter for a same-team pair). Matchup tables store champion_id_a's result specifically AGAINST
champion_id_b -- order matters, so both (a,b) and (b,a) rows are written by the aggregate job,
and get_*_matchup(conn, a, b) always returns a's win rate facing b.
"""
from __future__ import annotations

import sqlite3


def _ordered(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def replace_synergy(conn: sqlite3.Connection, table: str, rows: list[tuple[int, int, int, int]]) -> None:
    """table: 'synergy_pro' | 'synergy_roster'. rows: (champion_id_a, champion_id_b, games_together, wins_together),
    already canonically ordered by the caller."""
    assert table in ("synergy_pro", "synergy_roster")
    conn.execute(f"DELETE FROM {table}")
    conn.executemany(
        f"INSERT INTO {table} (champion_id_a, champion_id_b, games_together, wins_together, computed_at) "
        f"VALUES (?, ?, ?, ?, datetime('now'))",
        rows,
    )


def get_synergy(conn: sqlite3.Connection, table: str, champ_a: int, champ_b: int) -> dict | None:
    assert table in ("synergy_pro", "synergy_roster")
    a, b = _ordered(champ_a, champ_b)
    row = conn.execute(
        f"SELECT * FROM {table} WHERE champion_id_a = ? AND champion_id_b = ?", (a, b)
    ).fetchone()
    return dict(row) if row else None


def replace_matchup(conn: sqlite3.Connection, table: str, rows: list[tuple[int, int, int, int]]) -> None:
    """table: 'matchup_pro' | 'matchup_roster'. rows: (champion_id_a, champion_id_b, games, wins_a) --
    directional: this is a's record specifically against b. Caller must write BOTH directions
    ((a,b) and (b,a)) since they are not derivable from one another without knowing wins_a vs wins_b."""
    assert table in ("matchup_pro", "matchup_roster")
    conn.execute(f"DELETE FROM {table}")
    conn.executemany(
        f"INSERT INTO {table} (champion_id_a, champion_id_b, games, wins_a, computed_at) "
        f"VALUES (?, ?, ?, ?, datetime('now'))",
        rows,
    )


def get_matchup(conn: sqlite3.Connection, table: str, champ_a: int, champ_b: int) -> dict | None:
    """Returns champ_a's record facing champ_b: {"games": int, "wins_a": int}."""
    assert table in ("matchup_pro", "matchup_roster")
    row = conn.execute(
        f"SELECT * FROM {table} WHERE champion_id_a = ? AND champion_id_b = ?", (champ_a, champ_b)
    ).fetchone()
    return dict(row) if row else None
