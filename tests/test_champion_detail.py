"""Tests for scoring/champion_detail.py -- the hover-detail-panel data source. Uses the same
seeding conventions as test_scoring.py (real repository layer against an in-memory DB)."""
from __future__ import annotations

from draftassistant.db.repositories import (
    champion_repo,
    mastery_repo,
    personal_stats_repo,
    roster_repo,
    synergy_repo,
    tierlist_repo,
)
from draftassistant.draft.state import DraftState, RosterAssignment
from draftassistant.scoring.champion_detail import get_champion_detail

PATCH = "14.13"


def _insert_players(conn, count: int) -> None:
    for pid in range(1, count + 1):
        roster_repo.upsert_player(
            conn, display_name=f"Player{pid}", riot_game_name=f"P{pid}", riot_tag_line="NA1",
            platform_region="na1", account_region="americas",
        )


def _champion(conn, champion_id: int, name: str) -> None:
    champion_repo.upsert_champion(
        conn, champion_id=champion_id, champion_key=name, name=name, tags=[], icon_path="",
        ddragon_version="14.13.1",
    )


def _roster_lookup(conn) -> dict[int, dict]:
    return {p["player_id"]: p for p in roster_repo.get_active_players(conn)}


def _basic_roster() -> list[RosterAssignment]:
    return [
        RosterAssignment(player_id=1, role="TOP", side="BLUE"),
        RosterAssignment(player_id=2, role="JUNGLE", side="BLUE"),
        RosterAssignment(player_id=3, role="MID", side="BLUE"),
        RosterAssignment(player_id=4, role="BOTTOM", side="BLUE"),
        RosterAssignment(player_id=5, role="SUPPORT", side="BLUE"),
    ]


def test_fully_covered_champion_pick_context(test_db_conn):
    conn = test_db_conn
    _insert_players(conn, 5)
    _champion(conn, 1, "Ahri")
    _champion(conn, 2, "LeeSin")
    champion_repo.replace_role_eligibility(conn, "pro", [(1, "MID", 500)])
    tierlist_repo.upsert_tier_entry(
        conn, champion_id=1, role="MID", patch=PATCH, win_rate=0.532, pick_rate=0.081,
        ban_rate=0.043, tier="A", sample_size=14200, source_note="test",
    )
    mastery_repo.replace_player_mastery(
        conn, player_id=3, entries=[{"championId": 1, "championLevel": 7, "championPoints": 187543}],
    )
    personal_stats_repo.replace_all(conn, [(3, 1, "MID", 42, 27, 1_700_000_000_000)])
    synergy_repo.replace_synergy(conn, "synergy_roster", [(1, 2, 340, 189)])
    conn.commit()

    roster = _basic_roster()
    state = DraftState(our_side="BLUE", roster=roster)
    # Slots 0-5 are bans (per DRAFT_SEQUENCE); slot 6 is BLUE's first real pick. Advance through
    # the bans with dummy champions outside our test pool, then make our first real pick our
    # ally (champion 2) -- this leaves current_slot=7 (RED's pick slot, still PICK *context*
    # since action_context only cares about action type, not whose turn), with our_picks_so_far
    # correctly reflecting champion 2.
    for slot in range(6):
        state.enter(slot, champion_id=900 + slot)
    state.enter(6, champion_id=2)

    detail = get_champion_detail(conn, state, champion_id=1, roster_lookup=_roster_lookup(conn))

    assert detail["champion_id"] == 1
    assert detail["champion_name"] == "Ahri"
    assert detail["role"] == "MID"
    assert detail["role_source"] == "inferred"
    assert detail["action_context"] == "PICK"

    assert detail["global"]["has_data"] is True
    assert detail["global"]["win_rate"] == 0.532
    assert detail["global"]["pick_rate"] == 0.081
    assert detail["global"]["sample_size"] == 14200

    mid_player = next(r for r in detail["roster"] if r["player_id"] == 3)
    assert mid_player["is_assigned_to_role"] is True
    assert mid_player["mastery"] == {"level": 7, "points": 187543}
    assert mid_player["personal"] == {"games": 42, "wins": 27, "win_rate": 27 / 42}

    other_player = next(r for r in detail["roster"] if r["player_id"] == 1)
    assert other_player["mastery"] is None
    assert other_player["personal"] is None
    assert other_player["is_assigned_to_role"] is False

    assert detail["synergy_with_picks"] == [
        {"ally_champion_id": 2, "ally_champion_name": "LeeSin", "games_together": 340,
         "win_rate_together": 189 / 340, "source": "roster"}
    ]
    assert detail["ban_threat"] is None


def test_data_thin_champion_degrades_gracefully(test_db_conn):
    conn = test_db_conn
    _insert_players(conn, 5)
    _champion(conn, 99, "ObscureChamp")
    # No eligibility, no tier entry, no mastery, no personal stats, no synergy rows at all.
    conn.commit()

    state = DraftState(our_side="BLUE", roster=_basic_roster())
    detail = get_champion_detail(conn, state, champion_id=99, roster_lookup=_roster_lookup(conn))

    assert detail["role"] is None
    assert detail["global"] == {"win_rate": None, "pick_rate": None, "ban_rate": None,
                                 "tier": None, "sample_size": None, "has_data": False}
    assert len(detail["roster"]) == 5
    assert all(r["mastery"] is None and r["personal"] is None for r in detail["roster"])
    assert detail["synergy_with_picks"] == []


def test_ban_context_returns_threat_not_synergy(test_db_conn):
    conn = test_db_conn
    _insert_players(conn, 5)
    _champion(conn, 1, "Zed")
    _champion(conn, 2, "Ahri")  # player 3's comfort pick
    champion_repo.replace_role_eligibility(conn, "pro", [(1, "MID", 400)])
    tierlist_repo.upsert_tier_entry(
        conn, champion_id=1, role="MID", patch=PATCH, win_rate=0.50, pick_rate=0.10,
        ban_rate=0.09, tier="B", sample_size=5000, source_note="test",
    )
    mastery_repo.replace_player_mastery(
        conn, player_id=3, entries=[{"championId": 2, "championLevel": 7, "championPoints": 90000}],
    )
    synergy_repo.replace_matchup(conn, "matchup_roster", [(1, 2, 55, 34), (2, 1, 55, 21)])
    conn.commit()

    state = DraftState(our_side="BLUE", roster=_basic_roster())
    # Fill all 6 ban slots so we land on slot 6 which per DRAFT_SEQUENCE is a BLUE pick -- to
    # exercise the BAN branch instead, use a state still sitting on a BAN slot (slot 0).
    detail = get_champion_detail(conn, state, champion_id=1, roster_lookup=_roster_lookup(conn))

    assert detail["action_context"] == "BAN"
    assert detail["synergy_with_picks"] == []
    assert detail["ban_threat"] is not None
    threat = detail["ban_threat"]["counters_comfort_pool"]
    assert len(threat) == 1
    assert threat[0]["comfort_champion_name"] == "Ahri"
    # get_matchup(conn, table, 1, 2) returns champion_id_a=1's (Zed's) record facing
    # champion_id_b=2 (Ahri) -- the (1, 2, 55, 34) row inserted above, i.e. wins_a=34.
    assert threat[0]["win_rate_against"] == 34 / 55


def test_requested_role_falls_back_to_inference_when_invalid(test_db_conn):
    conn = test_db_conn
    _insert_players(conn, 5)
    _champion(conn, 1, "Ahri")
    champion_repo.replace_role_eligibility(conn, "pro", [(1, "MID", 500)])
    conn.commit()

    state = DraftState(our_side="BLUE", roster=_basic_roster())
    # Advance past the ban phase so we're genuinely in PICK context -- _relevant_role_for_candidate
    # (the PICK-context inference path) is what's actually exercised by suggestion-row hovering
    # in practice, unlike the BAN-context _best_tier_role fallback tested separately above.
    for slot in range(6):
        state.enter(slot, champion_id=900 + slot)

    # Ahri has no SUPPORT eligibility -- requesting it should fall back to inference, not be
    # trusted blindly.
    detail = get_champion_detail(
        conn, state, champion_id=1, roster_lookup=_roster_lookup(conn), requested_role="SUPPORT",
    )
    assert detail["role"] == "MID"
    assert detail["role_source"] == "inferred"

    detail_valid = get_champion_detail(
        conn, state, champion_id=1, roster_lookup=_roster_lookup(conn), requested_role="MID",
    )
    assert detail_valid["role"] == "MID"
    assert detail_valid["role_source"] == "requested"
