"""Derived pairwise synergy (same-team) and matchup (opposing-team) aggregates for OUR roster's
own match history -- same logic as ingest/oracles_elixir.py's pro versions, but grouped by
(match_id, team_id) instead of (gameid, teamid), sourced from match_participants instead of
oe_games_raw."""
from __future__ import annotations

from draftassistant import config
from draftassistant.db.repositories import match_repo, synergy_repo


def recompute_roster_synergy(conn) -> None:
    """Rebuilds synergy_roster and matchup_roster from match_participants, restricted to ranked
    queues (the same roster-participant base dataset personal_stats.py uses)."""
    rows = match_repo.get_all_roster_participants(conn, config.RANKED_QUEUE_IDS)

    _recompute_synergy(conn, rows)
    _recompute_matchup(conn, rows)


def _recompute_synergy(conn, rows: list[dict]) -> None:
    by_team: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        key = (r["match_id"], r["team_id"])
        by_team.setdefault(key, []).append(r)

    pair_stats: dict[tuple[int, int], list[int]] = {}
    for team_rows in by_team.values():
        n = len(team_rows)
        for i in range(n):
            for j in range(i + 1, n):
                a_row, b_row = team_rows[i], team_rows[j]
                a, b = sorted((a_row["champion_id"], b_row["champion_id"]))
                if a == b:
                    continue
                stats = pair_stats.setdefault((a, b), [0, 0])
                stats[0] += 1
                if a_row["win"]:
                    stats[1] += 1

    synergy_rows = [(a, b, games, wins) for (a, b), (games, wins) in pair_stats.items()]
    synergy_repo.replace_synergy(conn, "synergy_roster", synergy_rows)


def _recompute_matchup(conn, rows: list[dict]) -> None:
    by_match: dict[str, dict[int, list[dict]]] = {}
    for r in rows:
        match_teams = by_match.setdefault(r["match_id"], {})
        match_teams.setdefault(r["team_id"], []).append(r)

    pair_stats: dict[tuple[int, int], list[int]] = {}

    def _bump(a_champ: int, a_won: bool, b_champ: int) -> None:
        stats = pair_stats.setdefault((a_champ, b_champ), [0, 0])
        stats[0] += 1
        if a_won:
            stats[1] += 1

    for match_teams in by_match.values():
        team_ids = list(match_teams.keys())
        if len(team_ids) != 2:
            # Our own roster's participant rows for a given match only ever include our roster
            # players (per pre_draft_refresh.py's filter), so a match with roster players split
            # across only one team_id (5 of our own in the same match, e.g. a 5-stack) can't form
            # a cross-team matchup pair -- skip rather than guess.
            continue
        team_a_rows, team_b_rows = match_teams[team_ids[0]], match_teams[team_ids[1]]
        for a_row in team_a_rows:
            for b_row in team_b_rows:
                _bump(a_row["champion_id"], bool(a_row["win"]), b_row["champion_id"])
                _bump(b_row["champion_id"], bool(b_row["win"]), a_row["champion_id"])

    matchup_rows = [(a, b, games, wins_a) for (a, b), (games, wins_a) in pair_stats.items()]
    synergy_repo.replace_matchup(conn, "matchup_roster", matchup_rows)
