"""Oracle's Elixir pro-match CSV ingestion.

Entirely independent of the Riot API -- this is pure CSV -> DB. OE publishes one row per
player-per-game (plus one "team" summary row per team per game, which we filter out) with
columns including gameid, teamid, side, position, champion, result, ban1..ban5, etc.

https://oracleselixir.com/tools/downloads
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from draftassistant.db.repositories import champion_repo, oe_repo, synergy_repo

logger = logging.getLogger(__name__)

# OE's `champion` column is a free-text name string that doesn't always match DDragon's `name`
# field exactly. This is a small, deliberately incomplete alias map for known historical
# mismatches -- when a champion name fails to resolve during import, it's logged and counted
# (not crashed on), so add an entry here whenever the unresolved-name log surfaces a new one
# rather than trying to anticipate every possible OE naming quirk up front.
OE_NAME_ALIASES: dict[str, str] = {
    "Wukong": "MonkeyKing",  # OE sometimes uses the champion_key style name instead of display name
    "Nunu": "Nunu & Willump",  # older OE files predate the "& Willump" suffix
    "Renata": "Renata Glasc",  # some OE exports shorten this
}

# Normalizes OE's `position` column values to this project's TOP/JUNGLE/MID/BOTTOM/SUPPORT roles.
_POSITION_TO_ROLE = {
    "top": "TOP",
    "jng": "JUNGLE",
    "mid": "MID",
    "bot": "BOTTOM",
    "sup": "SUPPORT",
}


def _resolve_champion_id(conn, name: str) -> int | None:
    if not name or not isinstance(name, str):
        return None
    champ = champion_repo.get_champion_by_name(conn, name)
    if champ is not None:
        return champ["champion_id"]
    alias = OE_NAME_ALIASES.get(name)
    if alias is not None:
        champ = champion_repo.get_champion_by_name(conn, alias)
        if champ is not None:
            return champ["champion_id"]
    return None


def import_csv(conn, csv_path: Path) -> dict:
    """Loads an Oracle's Elixir CSV, filters to datacompleteness == 'complete', resolves each
    individual player row's champion name to champion_id, upserts into oe_games_raw, then
    recomputes the derived pro synergy/matchup/role-eligibility aggregates from the full
    (freshly-loaded) oe_games_raw table.

    Rows whose champion name fails to resolve are logged and skipped (not crashed on) --
    returned in the summary so the CLI can report how many rows need alias-map attention.
    """
    csv_path = Path(csv_path)
    df = pd.read_csv(csv_path, low_memory=False)
    df = df[df["datacompleteness"] == "complete"]

    player_rows = df[df["position"] != "team"]

    unresolved: list[str] = []
    row_dicts: list[dict] = []
    for _, row in player_rows.iterrows():
        champion_name_raw = row.get("champion")
        champion_id = _resolve_champion_id(conn, champion_name_raw)
        if champion_id is None:
            unresolved.append(str(champion_name_raw))
            logger.warning("Unresolved OE champion name %r in gameid=%s -- add to OE_NAME_ALIASES "
                            "if this is a legitimate mismatch.", champion_name_raw, row.get("gameid"))
            continue

        row_dicts.append({
            "gameid": row.get("gameid"),
            "playerid": row.get("playerid"),
            "participantid": row.get("participantid"),
            "side": row.get("side"),
            "position": row.get("position"),
            "playername": row.get("playername"),
            "teamname": row.get("teamname"),
            "teamid": row.get("teamid"),
            "champion_id": champion_id,
            "champion_name_raw": champion_name_raw,
            "ban1": row.get("ban1"), "ban2": row.get("ban2"), "ban3": row.get("ban3"),
            "ban4": row.get("ban4"), "ban5": row.get("ban5"),
            "result": row.get("result"),
            "league": row.get("league"),
            "year": row.get("year"),
            "split": row.get("split"),
            "playoffs": row.get("playoffs"),
            "date": row.get("date"),
            "patch": row.get("patch"),
            "gamelength": row.get("gamelength"),
            "datacompleteness": row.get("datacompleteness"),
            "source_file": csv_path.name,
        })

    # sqlite3 can't bind numpy/pandas scalar types directly for some dtypes (e.g. numpy.int64) --
    # normalize every value to a plain Python type before handing rows to the repo layer.
    row_dicts = [{k: _to_plain(v) for k, v in r.items()} for r in row_dicts]

    rows_upserted = oe_repo.upsert_rows(conn, row_dicts) if row_dicts else 0

    recompute_pro_synergy(conn)
    recompute_pro_matchup(conn)
    recompute_pro_role_eligibility(conn)

    games_count = player_rows["gameid"].nunique() if not player_rows.empty else 0
    teams_count = player_rows["teamid"].nunique() if not player_rows.empty else 0

    return {
        "rows_processed": len(player_rows),
        "rows_upserted": rows_upserted,
        "rows_unresolved": len(unresolved),
        "unresolved_champion_names": sorted(set(unresolved)),
        "games_count": int(games_count),
        "teams_count": int(teams_count),
    }


def _to_plain(value):
    """Converts pandas/numpy scalar types (and NaN) to plain Python types sqlite3 can bind."""
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def recompute_pro_synergy(conn) -> None:
    """Same-team champion pair co-occurrence, tallied per canonically-ordered
    (champion_id_a, champion_id_b) pair across all complete OE games."""
    rows = oe_repo.get_player_rows_complete(conn)

    by_team: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        key = (r["gameid"], r["teamid"])
        by_team.setdefault(key, []).append(r)

    pair_stats: dict[tuple[int, int], list[int]] = {}  # (a,b) -> [games, wins]
    for team_rows in by_team.values():
        n = len(team_rows)
        for i in range(n):
            for j in range(i + 1, n):
                a_row, b_row = team_rows[i], team_rows[j]
                a, b = sorted((a_row["champion_id"], b_row["champion_id"]))
                if a == b:
                    continue  # shouldn't happen (same champ twice on one team) but guard anyway
                stats = pair_stats.setdefault((a, b), [0, 0])
                stats[0] += 1
                if a_row["result"]:
                    stats[1] += 1

    synergy_rows = [(a, b, games, wins) for (a, b), (games, wins) in pair_stats.items()]
    synergy_repo.replace_synergy(conn, "synergy_pro", synergy_rows)


def recompute_pro_matchup(conn) -> None:
    """Cross-team champion pair matchup, tallied DIRECTIONALLY: for every ordered pair (a, b)
    where a and b were on opposing teams in the same game, write both (a, b) and (b, a) rows
    with each side's own win/loss recorded against the other."""
    rows = oe_repo.get_player_rows_complete(conn)

    by_game: dict[str, dict[str, list[dict]]] = {}
    for r in rows:
        game_teams = by_game.setdefault(r["gameid"], {})
        game_teams.setdefault(r["teamid"], []).append(r)

    pair_stats: dict[tuple[int, int], list[int]] = {}  # (a,b) directional -> [games, wins_a]

    def _bump(a_champ: int, a_won: bool, b_champ: int) -> None:
        stats = pair_stats.setdefault((a_champ, b_champ), [0, 0])
        stats[0] += 1
        if a_won:
            stats[1] += 1

    for game_teams in by_game.values():
        team_ids = list(game_teams.keys())
        if len(team_ids) != 2:
            continue  # malformed/incomplete game data -- skip rather than guess
        team_a_rows, team_b_rows = game_teams[team_ids[0]], game_teams[team_ids[1]]
        for a_row in team_a_rows:
            for b_row in team_b_rows:
                _bump(a_row["champion_id"], bool(a_row["result"]), b_row["champion_id"])
                _bump(b_row["champion_id"], bool(b_row["result"]), a_row["champion_id"])

    matchup_rows = [(a, b, games, wins_a) for (a, b), (games, wins_a) in pair_stats.items()]
    synergy_repo.replace_matchup(conn, "matchup_pro", matchup_rows)


def recompute_pro_role_eligibility(conn) -> None:
    """Counts games observed per (champion_id, normalized role) across all complete OE player
    rows, used as the 'pro' source for champion_role_eligibility."""
    rows = oe_repo.get_player_rows_complete(conn)

    counts: dict[tuple[int, str], int] = {}
    for r in rows:
        role = _POSITION_TO_ROLE.get(str(r["position"]).lower())
        if role is None:
            continue
        key = (r["champion_id"], role)
        counts[key] = counts.get(key, 0) + 1

    eligibility_rows = [(champ_id, role, games) for (champ_id, role), games in counts.items()]
    champion_repo.replace_role_eligibility(conn, "pro", eligibility_rows)
