"""Tests for draftassistant.scoring.pick_score / ban_score against a small synthetic DB built
via the real repository layer (test_db_conn fixture: in-memory SQLite, real schema, empty).

Covers:
  1. A fully-covered candidate's score/confidence match a hand-computed weighted average.
  2. A deliberately data-thin candidate (only a role-eligibility row) still appears in
     rank_pick_candidates' output with a low-but-defined confidence and a real (non-None/NaN)
     score, rather than crashing or being silently excluded.
  3. rank_ban_candidates produces a different top candidate for phase 1 (no enemy picks) vs.
     phase 2 (enemy has picks) given the same underlying data, demonstrating the weight-set
     switch (PHASE_1_WEIGHTS vs PHASE_2_WEIGHTS) actually takes effect.
"""
import math

import pytest

from draftassistant.db.repositories import (
    champion_repo,
    mastery_repo,
    personal_stats_repo,
    roster_repo,
    synergy_repo,
    tierlist_repo,
)
from draftassistant.draft.state import DraftState, RosterAssignment
from draftassistant.scoring.ban_score import rank_ban_candidates
from draftassistant.scoring.pick_score import (
    DEFAULT_PICK_WEIGHTS,
    rank_pick_candidates,
    score_with_confidence,
)

PATCH = "14.13"


def _insert_players(conn, count: int) -> None:
    for pid in range(1, count + 1):
        roster_repo.upsert_player(
            conn,
            display_name=f"Player{pid}",
            riot_game_name=f"P{pid}",
            riot_tag_line="NA1",
            platform_region="na1",
            account_region="americas",
        )


# ======================================================================================
# score_with_confidence -- the shared mechanism itself
# ======================================================================================


def test_score_with_confidence_full_coverage():
    weights = {"a": 0.6, "b": 0.4}
    score, confidence = score_with_confidence({"a": 1.0, "b": 0.0}, weights)
    assert score == 0.6
    assert confidence == 1.0


def test_score_with_confidence_partial_coverage_excludes_missing_not_defaults():
    weights = {"a": 0.5, "b": 0.5}
    score, confidence = score_with_confidence({"a": 1.0, "b": None}, weights)
    # 'b' must be excluded entirely, not defaulted to 0 -- so score is exactly 1.0, not 0.5.
    assert score == 1.0
    assert confidence == 0.5


def test_score_with_confidence_no_signals_returns_last_resort():
    weights = {"a": 0.5, "b": 0.5}
    score, confidence = score_with_confidence({"a": None, "b": None}, weights)
    assert score == 0.5
    assert confidence == 0.0


# ======================================================================================
# rank_pick_candidates
# ======================================================================================


def test_pick_scoring_fully_covered_candidate_matches_hand_computed_value(test_db_conn):
    conn = test_db_conn
    _insert_players(conn, 5)

    # Champions: 1 = fully-covered candidate (JUNGLE), 2/3 = tier-list competitors (JUNGLE).
    for cid, key, name in [(1, "ChampA", "Champ A"), (2, "ChampB", "Champ B"), (3, "ChampC", "Champ C")]:
        champion_repo.upsert_champion(
            conn, champion_id=cid, champion_key=key, name=name, tags=[], icon_path="",
            ddragon_version="14.13.1",
        )
    champion_repo.replace_role_eligibility(
        conn, "pro", [(1, "JUNGLE", 100), (2, "JUNGLE", 100), (3, "JUNGLE", 100)]
    )

    tierlist_repo.upsert_tier_entry(
        conn, champion_id=1, role="JUNGLE", patch=PATCH, win_rate=0.55, pick_rate=0.10,
        ban_rate=0.05, tier="S", sample_size=1000, source_note="test",
    )
    tierlist_repo.upsert_tier_entry(
        conn, champion_id=2, role="JUNGLE", patch=PATCH, win_rate=0.50, pick_rate=0.20,
        ban_rate=0.05, tier="A", sample_size=1000, source_note="test",
    )
    tierlist_repo.upsert_tier_entry(
        conn, champion_id=3, role="JUNGLE", patch=PATCH, win_rate=0.45, pick_rate=0.05,
        ban_rate=0.05, tier="B", sample_size=1000, source_note="test",
    )

    # Player 2 is assigned JUNGLE; give them a mastery distribution and personal stats on champ 1.
    mastery_repo.replace_player_mastery(
        conn,
        player_id=2,
        entries=[
            {"championId": 1, "championLevel": 7, "championPoints": 50000},
            {"championId": 2, "championLevel": 5, "championPoints": 20000},
            {"championId": 3, "championLevel": 3, "championPoints": 10000},
        ],
    )
    personal_stats_repo.replace_all(conn, [(2, 1, "JUNGLE", 10, 6, 1_700_000_000_000)])
    conn.commit()

    # Roster order: JUNGLE first, so unfilled_roles()[0] == "JUNGLE" while no picks exist yet.
    roster = [
        RosterAssignment(player_id=2, role="JUNGLE", side="BLUE"),
        RosterAssignment(player_id=1, role="TOP", side="BLUE"),
        RosterAssignment(player_id=3, role="MID", side="BLUE"),
        RosterAssignment(player_id=4, role="BOTTOM", side="BLUE"),
        RosterAssignment(player_id=5, role="SUPPORT", side="BLUE"),
    ]
    state = DraftState(our_side="BLUE", roster=roster)
    # Advance through the 6 ban slots (0-5) with dummy champions outside our candidate pool,
    # landing on slot 6: BLUE's first pick, opp_pick_gap_after == 2.
    for slot in range(6):
        state.enter(slot, champion_id=900 + slot)
    assert state.current_slot == 6

    results = rank_pick_candidates(conn, state, patch=PATCH, all_champion_ids=[1, 2, 3])
    candidate_a = next(r for r in results if r["champion_id"] == 1)

    # ---- Hand-computed expectation for candidate A (champion_id=1) ----
    # global_win_rate: min-max(0.55; [0.55,0.50,0.45]) = 1.0
    win_rates = [0.55, 0.50, 0.45]
    expected_win_rate = (0.55 - min(win_rates)) / (max(win_rates) - min(win_rates))
    # global_pick_rate: log-scale then min-max(log(0.10); log([0.10,0.20,0.05]))
    pick_rates = [0.10, 0.20, 0.05]
    log_pick_rates = [math.log(p) for p in pick_rates]
    expected_pick_rate = (math.log(0.10) - min(log_pick_rates)) / (max(log_pick_rates) - min(log_pick_rates))
    # personal_mastery: min-max(50000; [50000,20000,10000]) = 1.0
    mastery_points = [50000, 20000, 10000]
    expected_mastery = (50000 - min(mastery_points)) / (max(mastery_points) - min(mastery_points))
    # personal_win_rate: (6+5)/(10+10) = 0.55
    expected_personal_wr = (6 + 5) / (10 + 10)
    # pro_synergy / roster_synergy: no allies picked yet -> None (excluded)
    # role_need: relevant_role (JUNGLE) == unfilled[0] ("JUNGLE") -> 1.0
    expected_role_need = 1.0
    # pick_safety: gap==2 -> flexibility ** 1.5.
    #   flexibility = 0.5*multi_role_bonus + 0.5*pick_rate_proxy (renormalized) --
    #   champion 1 has exactly 1 eligible role -> multi_role_bonus = 0.0
    #   pick_rate_proxy reuses global_pick_rate for champ1's best tier role (JUNGLE) = expected_pick_rate
    expected_flexibility = (0.5 * 0.0 + 0.5 * expected_pick_rate) / (0.5 + 0.5)
    expected_pick_safety = expected_flexibility ** 1.5

    expected_breakdown = {
        "global_win_rate": expected_win_rate,
        "global_pick_rate": expected_pick_rate,
        "personal_mastery": expected_mastery,
        "personal_win_rate": expected_personal_wr,
        "pro_synergy": None,
        "roster_synergy": None,
        "role_need": expected_role_need,
        "pick_safety": expected_pick_safety,
    }
    expected_score, expected_confidence = score_with_confidence(expected_breakdown, DEFAULT_PICK_WEIGHTS)

    for key, expected_value in expected_breakdown.items():
        actual_value = candidate_a["breakdown"][key]
        if expected_value is None:
            assert actual_value is None, f"{key}: expected None, got {actual_value}"
        else:
            assert actual_value == pytest_approx(expected_value), f"{key}: {actual_value} != {expected_value}"

    assert candidate_a["score"] == pytest_approx(expected_score)
    assert candidate_a["confidence"] == pytest_approx(expected_confidence)
    # Sanity: with pro_synergy + roster_synergy (0.10 + 0.10 = 0.20 weight) excluded, confidence
    # should be exactly 0.8.
    assert candidate_a["confidence"] == pytest_approx(0.8)


def pytest_approx(value, tol=1e-9):
    """Small local helper so every call site doesn't need to spell out pytest.approx(..., abs=...)
    with a custom tolerance -- equivalent in behavior, just avoids repetition."""
    return pytest.approx(value, abs=tol)


def test_pick_scoring_data_thin_candidate_still_appears_with_low_confidence(test_db_conn):
    conn = test_db_conn
    _insert_players(conn, 5)

    # Champion 1: fully covered (as above, abbreviated). Champion 4: ONLY a role-eligibility
    # row -- no tier list entry, no mastery, no personal stats, no synergy data whatsoever.
    for cid, key, name in [(1, "ChampA", "Champ A"), (4, "ChampD", "Champ D")]:
        champion_repo.upsert_champion(
            conn, champion_id=cid, champion_key=key, name=name, tags=[], icon_path="",
            ddragon_version="14.13.1",
        )
    champion_repo.replace_role_eligibility(
        conn, "pro", [(1, "JUNGLE", 100), (4, "JUNGLE", 5)]
    )
    tierlist_repo.upsert_tier_entry(
        conn, champion_id=1, role="JUNGLE", patch=PATCH, win_rate=0.55, pick_rate=0.10,
        ban_rate=0.05, tier="S", sample_size=1000, source_note="test",
    )
    conn.commit()

    roster = [
        RosterAssignment(player_id=2, role="JUNGLE", side="BLUE"),
        RosterAssignment(player_id=1, role="TOP", side="BLUE"),
        RosterAssignment(player_id=3, role="MID", side="BLUE"),
        RosterAssignment(player_id=4, role="BOTTOM", side="BLUE"),
        RosterAssignment(player_id=5, role="SUPPORT", side="BLUE"),
    ]
    state = DraftState(our_side="BLUE", roster=roster)
    for slot in range(6):
        state.enter(slot, champion_id=900 + slot)

    results = rank_pick_candidates(conn, state, patch=PATCH, all_champion_ids=[1, 4])
    champion_ids_returned = {r["champion_id"] for r in results}
    assert 4 in champion_ids_returned, "data-thin champion must still appear in the ranking"

    candidate_d = next(r for r in results if r["champion_id"] == 4)
    # Only role_need (0.15) and pick_safety (0.10) can resolve for a champion with zero tier
    # list / mastery / personal-stats / synergy data -- confidence should be exactly 0.25.
    assert candidate_d["confidence"] == pytest_approx(0.25)
    assert candidate_d["confidence"] < 0.5  # "low"
    assert candidate_d["score"] is not None
    assert not math.isnan(candidate_d["score"])
    # role_need must still resolve (static eligibility metadata, always resolvable) even though
    # every other signal is missing.
    assert candidate_d["breakdown"]["role_need"] == 1.0
    assert candidate_d["breakdown"]["global_win_rate"] is None
    assert candidate_d["breakdown"]["personal_mastery"] is None
    assert candidate_d["breakdown"]["personal_win_rate"] is None
    assert candidate_d["breakdown"]["pro_synergy"] is None
    assert candidate_d["breakdown"]["roster_synergy"] is None


# ======================================================================================
# rank_ban_candidates -- phase 1 vs phase 2 weight-set switch
# ======================================================================================


def _build_ban_scoring_fixture(conn):
    """Shared setup for the phase1/phase2 ban comparison: a 'Counter' champion that strongly
    threatens our comfort pool (via matchup_roster) but has NO enemy_draft_trajectory data, and
    a 'Synergist' champion that strongly synergizes with the enemy's already-picked champion
    (via synergy_pro) but has NO comfort-pool matchup data. A 5th 'Filler' champion exists
    purely to give the tier list real min-max spread (see _min_max_scale: an all-tied tier list
    degrades every tier signal to None, which would hide the intended comparison) while
    candidates 2 and 3 themselves are kept tied on tier stats, so the ranking under test is
    driven entirely by counters_our_comfort_pool / enemy_draft_trajectory, not tier noise."""
    _insert_players(conn, 5)

    for cid, key, name in [
        (1, "Comfort", "Comfort Pick"),  # our comfort pool target -- not itself a ban candidate
        (2, "Counter", "Counter Pick"),
        (3, "Synergist", "Synergist Pick"),
        (4, "EnemyPick", "Enemy Pick"),
        (5, "Filler", "Filler Pick"),  # tier-list spread only -- not itself a ban candidate
    ]:
        champion_repo.upsert_champion(
            conn, champion_id=cid, champion_key=key, name=name, tags=[], icon_path="",
            ddragon_version="14.13.1",
        )
    champion_repo.replace_role_eligibility(
        conn, "pro", [(2, "TOP", 100), (3, "TOP", 100), (4, "JUNGLE", 100)]
    )

    # Candidates 2 and 3 are tied on tier stats; Filler (5) differs, giving the role's tier list
    # real spread so win_rate/pick_rate resolve to a real (tied, non-None) value for 2 and 3
    # rather than degrading to None across the board.
    tierlist_repo.upsert_tier_entry(
        conn, champion_id=2, role="TOP", patch=PATCH, win_rate=0.48, pick_rate=0.08,
        ban_rate=0.05, tier="B", sample_size=500, source_note="test",
    )
    tierlist_repo.upsert_tier_entry(
        conn, champion_id=3, role="TOP", patch=PATCH, win_rate=0.48, pick_rate=0.08,
        ban_rate=0.05, tier="B", sample_size=500, source_note="test",
    )
    tierlist_repo.upsert_tier_entry(
        conn, champion_id=5, role="TOP", patch=PATCH, win_rate=0.55, pick_rate=0.15,
        ban_rate=0.05, tier="S", sample_size=500, source_note="test",
    )

    # Player 1's #1 comfort pick by mastery points is champion_id=1 (not otherwise scored --
    # only used as the "comfort pool" target for the matchup lookup).
    mastery_repo.replace_player_mastery(
        conn, player_id=1, entries=[{"championId": 1, "championLevel": 7, "championPoints": 100000}]
    )
    for pid in range(2, 6):
        mastery_repo.replace_player_mastery(conn, player_id=pid, entries=[])

    # Counter (2) crushes our comfort pick (1) -- 18/20 = 90% win rate for the candidate's side.
    # Synergist (3) deliberately has NO matchup_roster row at all -- counters_our_comfort_pool
    # must resolve to None for it, not a fabricated 0.
    synergy_repo.replace_matchup(
        conn, "matchup_roster", [(2, 1, 20, 18), (1, 2, 20, 2)]  # both directions, per contract
    )
    # Synergist (3) has strong pro-data synergy with the enemy's pick (4) -- 27/30 = 90%.
    synergy_repo.replace_synergy(conn, "synergy_pro", [(3, 4, 30, 27)])

    conn.commit()

    return [
        RosterAssignment(player_id=1, role="TOP", side="BLUE"),
        RosterAssignment(player_id=2, role="JUNGLE", side="BLUE"),
        RosterAssignment(player_id=3, role="MID", side="BLUE"),
        RosterAssignment(player_id=4, role="BOTTOM", side="BLUE"),
        RosterAssignment(player_id=5, role="SUPPORT", side="BLUE"),
    ]


def test_ban_scoring_phase1_favors_counter_of_our_comfort_pool(test_db_conn):
    conn = test_db_conn
    roster = _build_ban_scoring_fixture(conn)

    state = DraftState(our_side="BLUE", roster=roster)  # slot 0: still phase 1, no enemy picks
    results = rank_ban_candidates(conn, state, patch=PATCH, all_champion_ids=[2, 3])

    assert results[0]["champion_id"] == 2, "phase 1 should rank the comfort-pool counter first"
    assert "enemy_draft_trajectory" not in results[0]["breakdown"], (
        "phase 1 must not even compute enemy_draft_trajectory -- structurally absent, not "
        "computed-and-discarded"
    )


def test_ban_scoring_phase2_favors_enemy_draft_trajectory_and_flips_the_ranking(test_db_conn):
    conn = test_db_conn
    roster = _build_ban_scoring_fixture(conn)

    state = DraftState(our_side="BLUE", roster=roster)
    dummy = iter(range(500, 600))
    for slot in range(6):  # bans 0-5
        state.enter(slot, next(dummy))
    state.enter(6, next(dummy))  # BLUE pick
    state.enter(7, 4)  # RED pick -- seeds their_picks_so_far with the enemy's champion
    state.enter(8, next(dummy))  # RED pick
    state.enter(9, next(dummy))  # BLUE pick
    state.enter(10, next(dummy))  # BLUE pick
    state.enter(11, next(dummy))  # RED pick
    assert state.current_slot == 12  # first phase-2 ban slot (RED ban)

    results = rank_ban_candidates(conn, state, patch=PATCH, all_champion_ids=[2, 3])

    assert results[0]["champion_id"] == 3, (
        "phase 2 should flip to favor the champion that synergizes with the enemy's "
        "already-declared draft trajectory"
    )
    assert "enemy_draft_trajectory" in results[0]["breakdown"]
    assert results[0]["breakdown"]["enemy_draft_trajectory"] == pytest_approx(0.9)


def test_ban_scoring_phase1_and_phase2_use_different_weight_sets(test_db_conn):
    """Direct check that the weight sets themselves differ and are applied -- independent of
    the specific ranking-flip scenario above."""
    from draftassistant.scoring.ban_score import PHASE_1_WEIGHTS, PHASE_2_WEIGHTS

    assert "enemy_draft_trajectory" not in PHASE_1_WEIGHTS
    assert "enemy_draft_trajectory" in PHASE_2_WEIGHTS
    assert abs(sum(PHASE_1_WEIGHTS.values()) - 1.0) < 1e-9
    assert abs(sum(PHASE_2_WEIGHTS.values()) - 1.0) < 1e-9
    assert PHASE_1_WEIGHTS["counters_our_comfort_pool"] > PHASE_2_WEIGHTS["counters_our_comfort_pool"]
    assert PHASE_2_WEIGHTS["enemy_draft_trajectory"] > PHASE_1_WEIGHTS.get("global_win_rate", 0)
