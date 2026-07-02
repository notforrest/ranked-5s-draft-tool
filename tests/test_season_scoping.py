"""Tests for the season-boundary filter in aggregate/personal_stats.py and
aggregate/roster_synergy.py -- the fix for the actual reported bug: a teammate's personal stats
showed a win from last season (2025) even though they haven't played that champion this season.
Fetch-time scoping alone can't fix this for matches already cached locally from before this
change (or from before a reset_match_history.py re-backfill), so the aggregation queries
themselves must exclude anything older than config.CURRENT_SEASON_START_EPOCH_MS."""
from __future__ import annotations

from draftassistant import config
from draftassistant.aggregate.personal_stats import recompute_personal_stats
from draftassistant.aggregate.roster_synergy import recompute_roster_synergy
from draftassistant.db.repositories import champion_repo, match_repo, roster_repo

_DAY_MS = 24 * 3600 * 1000
IN_SEASON_MS = config.CURRENT_SEASON_START_EPOCH_MS + 30 * _DAY_MS
LAST_SEASON_MS = config.CURRENT_SEASON_START_EPOCH_MS - 30 * _DAY_MS  # e.g. "season 2025"


def _seed_player(conn, display_name="Trajomon") -> int:
    return roster_repo.upsert_player(
        conn, display_name=display_name, riot_game_name=display_name, riot_tag_line="josh",
        platform_region="na1", account_region="americas",
    )


def _seed_champion(conn, champion_id: int, name: str) -> None:
    champion_repo.upsert_champion(
        conn, champion_id=champion_id, champion_key=name, name=name, tags=["Assassin"],
        icon_path=f"https://example.com/{name}.png", ddragon_version="14.13.1",
    )


def _insert_match(conn, match_id: str, game_creation_ms: int, queue_id: int = 420) -> None:
    match_repo.insert_match(
        conn, match_id=match_id, platform_region="na1", game_creation_ms=game_creation_ms,
        game_duration_s=1800, queue_id=queue_id, game_mode="CLASSIC", patch="14.13",
        raw_json_path="/dev/null",
    )


def _insert_participant(conn, match_id: str, puuid: str, player_id: int, champion_id: int,
                         role: str, win: bool, team_id: int = 100) -> None:
    match_repo.insert_participant(
        conn, match_id=match_id, puuid=puuid, player_id=player_id, champion_id=champion_id,
        role=role, win=win, kills=5, deaths=2, assists=8, team_id=team_id,
    )


def test_recompute_personal_stats_excludes_a_match_from_last_season(test_db_conn):
    """The exact reported bug: Trajomon has a win on Kha'Zix from last season, and no games on it
    this season -- personal_champion_stats must show zero games for that pairing, not 1."""
    player_id = _seed_player(test_db_conn)
    _seed_champion(test_db_conn, champion_id=121, name="Khazix")
    test_db_conn.commit()

    _insert_match(test_db_conn, "NA1_LAST_SEASON", LAST_SEASON_MS)
    _insert_participant(test_db_conn, "NA1_LAST_SEASON", "trajomon-puuid", player_id, 121, "JUNGLE", win=True)
    test_db_conn.commit()

    recompute_personal_stats(test_db_conn)

    stats_row = test_db_conn.execute(
        "SELECT * FROM personal_champion_stats WHERE player_id = ? AND champion_id = 121",
        (player_id,),
    ).fetchone()
    assert stats_row is None


def test_recompute_personal_stats_includes_an_in_season_match(test_db_conn):
    """Sanity check for the above: an in-season game on the same champion must still count --
    the fix should exclude only prior-season data, not break current-season aggregation."""
    player_id = _seed_player(test_db_conn)
    _seed_champion(test_db_conn, champion_id=121, name="Khazix")
    test_db_conn.commit()

    _insert_match(test_db_conn, "NA1_THIS_SEASON", IN_SEASON_MS)
    _insert_participant(test_db_conn, "NA1_THIS_SEASON", "trajomon-puuid", player_id, 121, "JUNGLE", win=True)
    test_db_conn.commit()

    recompute_personal_stats(test_db_conn)

    stats_row = test_db_conn.execute(
        "SELECT * FROM personal_champion_stats WHERE player_id = ? AND champion_id = 121",
        (player_id,),
    ).fetchone()
    assert stats_row is not None
    assert stats_row["games"] == 1
    assert stats_row["wins"] == 1


def test_recompute_personal_stats_mixed_seasons_counts_only_this_season(test_db_conn):
    """A champion with games in both seasons must be counted using only the in-season ones."""
    player_id = _seed_player(test_db_conn)
    _seed_champion(test_db_conn, champion_id=121, name="Khazix")
    test_db_conn.commit()

    _insert_match(test_db_conn, "NA1_LAST_SEASON", LAST_SEASON_MS)
    _insert_participant(test_db_conn, "NA1_LAST_SEASON", "trajomon-puuid", player_id, 121, "JUNGLE", win=True)
    _insert_match(test_db_conn, "NA1_THIS_SEASON_A", IN_SEASON_MS)
    _insert_participant(test_db_conn, "NA1_THIS_SEASON_A", "trajomon-puuid", player_id, 121, "JUNGLE", win=False)
    _insert_match(test_db_conn, "NA1_THIS_SEASON_B", IN_SEASON_MS + _DAY_MS)
    _insert_participant(test_db_conn, "NA1_THIS_SEASON_B", "trajomon-puuid", player_id, 121, "JUNGLE", win=False)
    test_db_conn.commit()

    recompute_personal_stats(test_db_conn)

    stats_row = test_db_conn.execute(
        "SELECT * FROM personal_champion_stats WHERE player_id = ? AND champion_id = 121",
        (player_id,),
    ).fetchone()
    assert stats_row["games"] == 2
    assert stats_row["wins"] == 0


def test_recompute_personal_stats_role_eligibility_also_excludes_last_season(test_db_conn):
    """champion_role_eligibility(source='personal') is derived from the same query -- a role only
    ever played last season shouldn't count as current empirical role eligibility either."""
    player_id = _seed_player(test_db_conn)
    _seed_champion(test_db_conn, champion_id=121, name="Khazix")
    test_db_conn.commit()

    _insert_match(test_db_conn, "NA1_LAST_SEASON", LAST_SEASON_MS)
    _insert_participant(test_db_conn, "NA1_LAST_SEASON", "trajomon-puuid", player_id, 121, "JUNGLE", win=True)
    test_db_conn.commit()

    recompute_personal_stats(test_db_conn)

    row = test_db_conn.execute(
        "SELECT * FROM champion_role_eligibility WHERE champion_id = 121 AND source = 'personal'"
    ).fetchone()
    assert row is None


def test_recompute_roster_synergy_excludes_a_pair_only_seen_last_season(test_db_conn):
    """Same fix applied to synergy_roster/matchup_roster: a same-team pairing only observed last
    season must not appear as a current synergy signal."""
    player_a = _seed_player(test_db_conn, display_name="Trajomon")
    player_b = _seed_player(test_db_conn, display_name="Teammate")
    _seed_champion(test_db_conn, champion_id=121, name="Khazix")
    _seed_champion(test_db_conn, champion_id=147, name="Seraphine")
    test_db_conn.commit()

    _insert_match(test_db_conn, "NA1_LAST_SEASON", LAST_SEASON_MS)
    _insert_participant(test_db_conn, "NA1_LAST_SEASON", "a-puuid", player_a, 121, "JUNGLE", win=True, team_id=100)
    _insert_participant(test_db_conn, "NA1_LAST_SEASON", "b-puuid", player_b, 147, "SUPPORT", win=True, team_id=100)
    test_db_conn.commit()

    recompute_roster_synergy(test_db_conn)

    row = test_db_conn.execute(
        "SELECT * FROM synergy_roster WHERE champion_id_a = 121 AND champion_id_b = 147"
    ).fetchone()
    assert row is None


def test_recompute_roster_synergy_includes_an_in_season_pair(test_db_conn):
    player_a = _seed_player(test_db_conn, display_name="Trajomon")
    player_b = _seed_player(test_db_conn, display_name="Teammate")
    _seed_champion(test_db_conn, champion_id=121, name="Khazix")
    _seed_champion(test_db_conn, champion_id=147, name="Seraphine")
    test_db_conn.commit()

    _insert_match(test_db_conn, "NA1_THIS_SEASON", IN_SEASON_MS)
    _insert_participant(test_db_conn, "NA1_THIS_SEASON", "a-puuid", player_a, 121, "JUNGLE", win=True, team_id=100)
    _insert_participant(test_db_conn, "NA1_THIS_SEASON", "b-puuid", player_b, 147, "SUPPORT", win=True, team_id=100)
    test_db_conn.commit()

    recompute_roster_synergy(test_db_conn)

    row = test_db_conn.execute(
        "SELECT * FROM synergy_roster WHERE champion_id_a = 121 AND champion_id_b = 147"
    ).fetchone()
    assert row is not None
    assert row["games_together"] == 1
    assert row["wins_together"] == 1
