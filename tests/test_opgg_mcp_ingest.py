"""Tests for ingest/opgg_mcp_ingest.py. Mocks at the opgg_mcp_client wrapper-function boundary
(already tested directly in test_opgg_mcp_client.py) rather than the raw MCP protocol -- these
tests are about the INGESTION logic (name resolution, DB writes, win-count math, error
isolation), not the client itself."""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from draftassistant.db.repositories import champion_repo, roster_repo, summoner_rank_repo, synergy_repo, tierlist_repo
from draftassistant.ingest import opgg_mcp_ingest
from draftassistant.staticdata.opgg_mcp_client import (
    ChampionAllySynergy,
    ChampionAnalysis,
    ChampionCounter,
    LaneMetaChampion,
    OpggMcpToolError,
    SummonerLeagueEntry,
    SummonerProfile,
)


def _champion(conn, champion_id, name):
    champion_repo.upsert_champion(
        conn, champion_id=champion_id, champion_key=name, name=name, tags=[], icon_path="",
        ddragon_version="14.13.1",
    )


@asynccontextmanager
async def _fake_session():
    yield object()  # never actually used -- the wrapper functions themselves are monkeypatched


def _patch_session(monkeypatch):
    monkeypatch.setattr(opgg_mcp_ingest.opgg_mcp_client, "opgg_session", _fake_session)


# ------------------------------------------------------------------
# platform_region_to_opgg_region
# ------------------------------------------------------------------


def test_platform_region_to_opgg_region_known_mapping():
    assert opgg_mcp_ingest.platform_region_to_opgg_region("kr") == "KR"
    assert opgg_mcp_ingest.platform_region_to_opgg_region("na1") == "NA"
    assert opgg_mcp_ingest.platform_region_to_opgg_region("euw1") == "EUW"


def test_platform_region_to_opgg_region_unknown_falls_back_to_stripped_uppercase():
    assert opgg_mcp_ingest.platform_region_to_opgg_region("newregion7") == "NEWREGION"


# ------------------------------------------------------------------
# import_lane_meta
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_lane_meta_resolves_champions_and_populates_ban_rate(test_db_conn, monkeypatch):
    conn = test_db_conn
    _champion(conn, 103, "Ahri")
    _champion(conn, 86, "Garen")
    tierlist_repo.upsert_tier_entry(
        conn, champion_id=103, role="MID", patch="14.13", win_rate=0.5, pick_rate=0.08,
        ban_rate=None, tier="A", sample_size=1000, source_note="opgg",
    )
    conn.commit()
    _patch_session(monkeypatch)

    async def fake_list_lane_meta_champions(session, *, position="all"):
        return [
            LaneMetaChampion(champion_name="Ahri", lane="MID", win_rate=0.51, pick_rate=0.09,
                              ban_rate=0.03, tier=1, kda=2.5, games_played=100000, rank=1),
            LaneMetaChampion(champion_name="Garen", lane="TOP", win_rate=0.52, pick_rate=0.08,
                              ban_rate=0.07, tier=1, kda=1.8, games_played=90000, rank=1),
            LaneMetaChampion(champion_name="TotallyUnknownChamp", lane="MID", win_rate=0.5,
                              pick_rate=0.01, ban_rate=0.0, tier=5, kda=1.0, games_played=100, rank=50),
        ]
    monkeypatch.setattr(opgg_mcp_ingest.opgg_mcp_client, "list_lane_meta_champions", fake_list_lane_meta_champions)

    result = await opgg_mcp_ingest.import_lane_meta(conn)

    assert result["entries_imported"] == 2
    assert result["patch"] == "14.13"  # reused the pre-existing patch, not a new/different one
    assert result["champions_unresolved"] == ["TotallyUnknownChamp"]

    ahri_entry = tierlist_repo.get_tier_entry(conn, 103, "MID", "14.13")
    assert ahri_entry["ban_rate"] == 0.03
    assert ahri_entry["source_note"] == "opgg-mcp"

    garen_entry = tierlist_repo.get_tier_entry(conn, 86, "TOP", "14.13")
    assert garen_entry["ban_rate"] == 0.07


@pytest.mark.asyncio
async def test_import_lane_meta_populates_role_eligibility(test_db_conn, monkeypatch):
    """Found live during this prototype's own smoke test: without this, a champion with only
    opgg-mcp tier-list data resolves to role=None everywhere downstream (get_champion_detail,
    suggestion role-gating), blanking out win/pick/ban rate display entirely, not just ban_rate."""
    conn = test_db_conn
    _champion(conn, 103, "Ahri")
    conn.commit()
    _patch_session(monkeypatch)

    async def fake_list_lane_meta_champions(session, *, position="all"):
        return [
            LaneMetaChampion(champion_name="Ahri", lane="MID", win_rate=0.51, pick_rate=0.09,
                              ban_rate=0.03, tier=1, kda=2.5, games_played=127761, rank=1),
        ]
    monkeypatch.setattr(opgg_mcp_ingest.opgg_mcp_client, "list_lane_meta_champions", fake_list_lane_meta_champions)

    await opgg_mcp_ingest.import_lane_meta(conn)

    eligibility = champion_repo.get_role_eligibility(conn, 103)
    assert eligibility["MID"] == {"source": "opgg-mcp", "games_observed": 127761}


@pytest.mark.asyncio
async def test_import_lane_meta_uses_unknown_patch_when_no_prior_data(test_db_conn, monkeypatch):
    conn = test_db_conn
    _patch_session(monkeypatch)
    monkeypatch.setattr(opgg_mcp_ingest.opgg_mcp_client, "list_lane_meta_champions", lambda session, **kw: _empty_list())

    result = await opgg_mcp_ingest.import_lane_meta(conn)
    assert result["patch"] == "unknown"
    assert result["entries_imported"] == 0


async def _empty_list():
    return []


# ------------------------------------------------------------------
# import_summoner_ranks
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_summoner_ranks_populates_summoner_rank_table(test_db_conn, monkeypatch):
    conn = test_db_conn
    player_id = roster_repo.upsert_player(
        conn, display_name="Faker", riot_game_name="Hide on bush", riot_tag_line="KR1",
        platform_region="kr", account_region="asia", preferred_roles=[],
    )
    conn.commit()
    _patch_session(monkeypatch)

    async def fake_get_summoner_profile(session, *, game_name, tag_line, region):
        assert game_name == "Hide on bush"
        assert tag_line == "KR1"
        assert region == "KR"  # confirms platform_region "kr" -> OP.GG region "KR" was applied
        return SummonerProfile(
            game_name=game_name, tagline=tag_line, level=918,
            league_entries=[
                SummonerLeagueEntry(game_type="SOLORANKED", tier="GRANDMASTER", division=1,
                                     lp=1713, wins=290, losses=240),
                SummonerLeagueEntry(game_type="FLEXRANKED", tier=None, division=None,
                                     lp=None, wins=None, losses=None),
            ],
            champion_pool=[],
        )
    monkeypatch.setattr(opgg_mcp_ingest.opgg_mcp_client, "get_summoner_profile", fake_get_summoner_profile)

    result = await opgg_mcp_ingest.import_summoner_ranks(conn)

    assert result["players"][0]["status"] == "ok"
    assert result["players"][0]["queues_found"] == 2
    solo_rank = summoner_rank_repo.get_player_rank(conn, player_id, "SOLORANKED")
    assert solo_rank["tier"] == "GRANDMASTER"
    assert solo_rank["lp"] == 1713


@pytest.mark.asyncio
async def test_import_summoner_ranks_isolates_per_player_failures(test_db_conn, monkeypatch):
    conn = test_db_conn
    p1 = roster_repo.upsert_player(
        conn, display_name="Alice", riot_game_name="Alice", riot_tag_line="NA1",
        platform_region="na1", account_region="americas", preferred_roles=[],
    )
    p2 = roster_repo.upsert_player(
        conn, display_name="Bob", riot_game_name="Bob", riot_tag_line="NA1",
        platform_region="na1", account_region="americas", preferred_roles=[],
    )
    conn.commit()
    _patch_session(monkeypatch)

    async def fake_get_summoner_profile(session, *, game_name, tag_line, region):
        if game_name == "Alice":
            raise OpggMcpToolError("lol_get_summoner_profile", "summoner not found")
        return SummonerProfile(game_name=game_name, tagline=tag_line, level=100, league_entries=[], champion_pool=[])
    monkeypatch.setattr(opgg_mcp_ingest.opgg_mcp_client, "get_summoner_profile", fake_get_summoner_profile)

    result = await opgg_mcp_ingest.import_summoner_ranks(conn)

    by_player = {r["player_id"]: r for r in result["players"]}
    assert by_player[p1]["status"] == "error"
    assert by_player[p2]["status"] == "ok"
    # Bob's rank rows were still written despite Alice's failure.
    assert summoner_rank_repo.get_all_ranks_for_player(conn, p2) == []  # no queues in the fake (empty league_entries)
    assert summoner_rank_repo.get_player_rank(conn, p1) is None


# ------------------------------------------------------------------
# import_champion_matchups_and_synergies
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_champion_matchups_computes_both_directions_correctly(test_db_conn, monkeypatch):
    """counter.win_rate is the COUNTER's win rate against the analyzed champion (see
    opgg_mcp_client._distribute_wins's docstring) -- confirms the math: Ahri (2592 games vs
    Yone, Yone wins 56%) must produce matchup_opgg rows where Yone's wins_a = round(0.56*2592)
    and Ahri's wins_a = 2592 - that, not the other way around."""
    conn = test_db_conn
    _champion(conn, 103, "Ahri")
    _champion(conn, 777, "Yone")
    conn.commit()
    _patch_session(monkeypatch)

    async def fake_get_champion_analysis(session, *, champion, position):
        return ChampionAnalysis(
            champion_name=champion, position="MID", win_rate=0.44, pick_rate=0.09, ban_rate=0.03, tier=1,
            strong_counters=[ChampionCounter(champion_name="Yone", win_rate=0.56, games=2592)],
            weak_counters=[], synergies=[],
        )
    monkeypatch.setattr(opgg_mcp_ingest.opgg_mcp_client, "get_champion_analysis", fake_get_champion_analysis)

    result = await opgg_mcp_ingest.import_champion_matchups_and_synergies(conn, [("Ahri", "mid")])

    assert result["champions_analyzed"] == 1
    assert result["matchup_pairs"] == 1

    yone_vs_ahri = synergy_repo.get_matchup(conn, "matchup_opgg", 777, 103)
    ahri_vs_yone = synergy_repo.get_matchup(conn, "matchup_opgg", 103, 777)
    assert yone_vs_ahri["games"] == 2592
    assert yone_vs_ahri["wins_a"] == round(0.56 * 2592)
    assert ahri_vs_yone["wins_a"] == 2592 - round(0.56 * 2592)


@pytest.mark.asyncio
async def test_import_champion_matchups_writes_synergy_rows(test_db_conn, monkeypatch):
    conn = test_db_conn
    _champion(conn, 103, "Ahri")
    _champion(conn, 875, "Sylas")
    conn.commit()
    _patch_session(monkeypatch)

    async def fake_get_champion_analysis(session, *, champion, position):
        return ChampionAnalysis(
            champion_name=champion, position="MID", win_rate=0.5, pick_rate=0.09, ban_rate=0.03, tier=1,
            strong_counters=[], weak_counters=[],
            synergies=[ChampionAllySynergy(ally_position="JUNGLE", ally_champion_name="Sylas",
                                            win_rate=0.54, games=820)],
        )
    monkeypatch.setattr(opgg_mcp_ingest.opgg_mcp_client, "get_champion_analysis", fake_get_champion_analysis)

    await opgg_mcp_ingest.import_champion_matchups_and_synergies(conn, [("Ahri", "mid")])

    synergy = synergy_repo.get_synergy(conn, "synergy_opgg", 103, 875)
    assert synergy["games_together"] == 820
    assert synergy["wins_together"] == round(0.54 * 820)


@pytest.mark.asyncio
async def test_import_champion_matchups_skips_unresolved_champion_in_the_requested_list(test_db_conn, monkeypatch):
    conn = test_db_conn
    _patch_session(monkeypatch)
    called = False

    async def fake_get_champion_analysis(session, *, champion, position):
        nonlocal called
        called = True
        raise AssertionError("should never be called for an unresolvable champion")
    monkeypatch.setattr(opgg_mcp_ingest.opgg_mcp_client, "get_champion_analysis", fake_get_champion_analysis)

    result = await opgg_mcp_ingest.import_champion_matchups_and_synergies(conn, [("NotARealChampion", "mid")])

    assert called is False
    assert result["champions_analyzed"] == 0
    assert "NotARealChampion" in result["champions_unresolved"]


@pytest.mark.asyncio
async def test_import_champion_matchups_isolates_per_champion_tool_failures(test_db_conn, monkeypatch):
    conn = test_db_conn
    _champion(conn, 103, "Ahri")
    _champion(conn, 86, "Garen")
    conn.commit()
    _patch_session(monkeypatch)

    async def fake_get_champion_analysis(session, *, champion, position):
        if champion == "Ahri":
            raise OpggMcpToolError("lol_get_champion_analysis", "rate limited")
        return ChampionAnalysis(
            champion_name=champion, position="TOP", win_rate=0.5, pick_rate=0.08, ban_rate=0.07,
            tier=1, strong_counters=[], weak_counters=[], synergies=[],
        )
    monkeypatch.setattr(opgg_mcp_ingest.opgg_mcp_client, "get_champion_analysis", fake_get_champion_analysis)

    result = await opgg_mcp_ingest.import_champion_matchups_and_synergies(
        conn, [("Ahri", "mid"), ("Garen", "top")],
    )
    assert result["champions_analyzed"] == 1  # Garen only -- Ahri's failure didn't abort the batch


# ------------------------------------------------------------------
# default_champion_positions_from_lane_meta
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_champion_positions_from_lane_meta_picks_top_n_by_sample_size(test_db_conn):
    conn = test_db_conn
    _champion(conn, 103, "Ahri")
    _champion(conn, 86, "Garen")
    tierlist_repo.upsert_tier_entry(
        conn, champion_id=103, role="MID", patch="14.13", win_rate=0.51, pick_rate=0.09,
        ban_rate=0.03, tier="1", sample_size=100000, source_note="opgg-mcp",
    )
    tierlist_repo.upsert_tier_entry(
        conn, champion_id=86, role="MID", patch="14.13", win_rate=0.5, pick_rate=0.01,
        ban_rate=0.0, tier="5", sample_size=10, source_note="opgg-mcp",
    )
    conn.commit()

    pairs = await opgg_mcp_ingest.default_champion_positions_from_lane_meta(conn, top_n_per_lane=1)
    mid_pairs = [p for p in pairs if p[1] == "mid"]
    assert mid_pairs == [("Ahri", "mid")]  # higher sample_size wins the top-1 slot over Garen


@pytest.mark.asyncio
async def test_default_champion_positions_from_lane_meta_empty_when_no_patch(test_db_conn):
    conn = test_db_conn
    pairs = await opgg_mcp_ingest.default_champion_positions_from_lane_meta(conn)
    assert pairs == []
