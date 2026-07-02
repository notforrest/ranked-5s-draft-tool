"""Ban-suggestion scoring.

Scope note (same as pick_score.py): this is a single-ply heuristic scorer, re-run fresh after
every `enter`/`undo`, NOT a game-theoretic search over the remaining draft tree. It reasons
about "what threatens us right now" (comfort-pool counters) and "what's dangerous in the
abstract" (global win/pick rate, enemy role flexibility, enemy's already-declared synergy
direction) -- it never simulates multiple future ban/pick exchanges. A minimax/MCTS layer
built on top of this as a leaf evaluator is a possible future extension, not attempted here.

Uses the same available-case weighted renormalization mechanism as pick scoring
(`pick_score.score_with_confidence`) -- imported from there rather than duplicated.
"""
from __future__ import annotations

import sqlite3

from draftassistant.config import VALID_ROLES
from draftassistant.db.repositories import champion_repo, mastery_repo, synergy_repo
from draftassistant.draft import queries
from draftassistant.draft.state import DraftState
from draftassistant.scoring.pick_score import (
    _average_synergy,
    _best_tier_role,
    _role_tier_signals,
    score_with_confidence,
)

# Phase 1 (slots 0-5, before any enemy picks exist): enemy_draft_trajectory is excluded
# entirely from the weight set (not weighted at 0) since there is nothing to compute it from.
PHASE_1_WEIGHTS: dict[str, float] = {
    "global_win_rate": 0.30,
    "global_pick_rate": 0.20,
    "enemy_role_flex_risk": 0.15,
    "counters_our_comfort_pool": 0.35,
}
assert abs(sum(PHASE_1_WEIGHTS.values()) - 1.0) < 1e-9

# Phase 2 (slots 12-15, after enemy has picks on the board): real information to react to.
PHASE_2_WEIGHTS: dict[str, float] = {
    "global_win_rate": 0.15,
    "global_pick_rate": 0.10,
    "enemy_role_flex_risk": 0.15,
    "counters_our_comfort_pool": 0.20,
    "enemy_draft_trajectory": 0.40,
}
assert abs(sum(PHASE_2_WEIGHTS.values()) - 1.0) < 1e-9


def _enemy_role_flex_risk_phase1(
    conn: sqlite3.Connection, champion_id: int
) -> float | None:
    """Phase 1: coarse -- nothing enemy-specific is known yet (no enemy picks exist), so this
    is just a role_need-style signal evaluated against ALL 5 standard roles rather than any
    team's actual remaining need. 1.0 if the candidate has ANY observed role-eligibility at all
    (every eligibility row is, by construction, tagged with one of the 5 standard roles, so
    having any row at all trivially means the candidate fits into the enemy's undifferentiated
    5-role need); None if the champion has no eligibility rows whatsoever, so there's nothing
    to evaluate."""
    eligibility = champion_repo.get_role_eligibility(conn, champion_id)
    if not eligibility:
        return None
    return 1.0


def _their_unfilled_roles(state: DraftState, conn: sqlite3.Connection) -> list[str]:
    """Mirrors queries.unfilled_roles, but for the enemy side: we don't have their per-player
    role assignments, so infer purely from their picks-so-far vs. the 5 standard roles."""
    remaining_roles = set(VALID_ROLES)
    for champion_id in queries.their_picks_so_far(state):
        eligibility = champion_repo.get_role_eligibility(conn, champion_id)
        candidates = {role: info for role, info in eligibility.items() if role in remaining_roles}
        if candidates:
            matched_role = max(candidates, key=lambda r: candidates[r]["games_observed"])
            remaining_roles.discard(matched_role)
    # Stable ordering matching VALID_ROLES.
    return [r for r in VALID_ROLES if r in remaining_roles]


def _enemy_role_flex_risk_phase2(
    conn: sqlite3.Connection, champion_id: int, state: DraftState
) -> float | None:
    """Phase 2: precise -- against the enemy's actual inferred unfilled roles (from their
    picks-so-far, assuming they're filling the same 5 standard roles). Same 1.0-fills-top-need
    / 0.5-flex-fits-another-need / None-no-eligibility-data shape as pick scoring's role_need."""
    their_unfilled = _their_unfilled_roles(state, conn)
    eligibility = champion_repo.get_role_eligibility(conn, champion_id)
    if not eligibility:
        return None
    if not their_unfilled:
        return 1.0  # enemy has no roles left to fill (their draft is role-complete already)
    candidates = {role: info for role, info in eligibility.items() if role in their_unfilled}
    if not candidates:
        return None  # candidate doesn't fit any of the enemy's remaining needs at all
    best_role = max(candidates, key=lambda r: candidates[r]["games_observed"])
    return 1.0 if best_role == their_unfilled[0] else 0.5


def _counters_our_comfort_pool(
    conn: sqlite3.Connection, table: str, state: DraftState, candidate: int
) -> float | None:
    """For each of our 5 roster players, take their top-3 champions by mastery points (already
    sorted desc by get_player_mastery_distribution) and look up the CANDIDATE's matchup record
    against each: a higher wins_a rate (candidate's team winning) means the candidate is more
    threatening to that comfort-pick. Averaged (games-weighted) across every found matchup;
    None if no matchup rows exist for any of the comfort-pool champions at all.

    `table`: 'matchup_pro' | 'matchup_roster'.
    """
    total_games = 0
    total_wins_a = 0.0
    for assignment in state.roster:
        distribution = mastery_repo.get_player_mastery_distribution(conn, assignment.player_id)
        top_3 = [d["champion_id"] for d in distribution[:3]]
        for their_comfort_champ in top_3:
            row = synergy_repo.get_matchup(conn, table, candidate, their_comfort_champ)
            if row is None or row["games"] <= 0:
                continue
            total_games += row["games"]
            total_wins_a += row["wins_a"]
    if total_games == 0:
        return None
    return total_wins_a / total_games


def rank_ban_candidates(
    conn: sqlite3.Connection,
    state: DraftState,
    patch: str,
    all_champion_ids: list[int] | None = None,
) -> list[dict]:
    """Rank every not-yet-used candidate champion as a ban suggestion for the current slot.
    Uses PHASE_1_WEIGHTS while the enemy has made no picks yet, PHASE_2_WEIGHTS once they have
    -- the exact same underlying signals are computed either way except enemy_draft_trajectory,
    which is structurally absent (never computed, not computed-and-discarded) in phase 1."""
    if all_champion_ids is None:
        all_champion_ids = [c["champion_id"] for c in champion_repo.get_all_champions(conn)]

    their_picks = queries.their_picks_so_far(state)
    is_phase_2 = len(their_picks) > 0
    weights = PHASE_2_WEIGHTS if is_phase_2 else PHASE_1_WEIGHTS

    available = queries.available_champions(state, all_champion_ids)

    matchup_table = "matchup_roster"  # our own roster's comfort pool is always roster-scoped
    synergy_pro_table = "synergy_pro"  # enemy_draft_trajectory reuses pro-data pairwise synergy,
    # mirroring pro_synergy in pick scoring -- same rationale: pro data is the broadest sample
    # of real team-comp synergy we have, independent of which specific players are on our roster.

    results: list[dict] = []
    for champion_id in available:
        best_role = _best_tier_role(conn, champion_id, patch)
        global_win_rate = None
        global_pick_rate = None
        if best_role is not None:
            global_win_rate, global_pick_rate = _role_tier_signals(conn, champion_id, best_role, patch)

        if is_phase_2:
            enemy_role_flex_risk = _enemy_role_flex_risk_phase2(conn, champion_id, state)
            enemy_draft_trajectory = _average_synergy(conn, synergy_pro_table, their_picks, champion_id)
        else:
            enemy_role_flex_risk = _enemy_role_flex_risk_phase1(conn, champion_id)
            enemy_draft_trajectory = None  # structurally absent in phase 1, never computed

        counters_our_comfort_pool = _counters_our_comfort_pool(conn, matchup_table, state, champion_id)

        breakdown = {
            "global_win_rate": global_win_rate,
            "global_pick_rate": global_pick_rate,
            "enemy_role_flex_risk": enemy_role_flex_risk,
            "counters_our_comfort_pool": counters_our_comfort_pool,
        }
        if is_phase_2:
            breakdown["enemy_draft_trajectory"] = enemy_draft_trajectory

        score, confidence = score_with_confidence(breakdown, weights)

        results.append(
            {
                "champion_id": champion_id,
                "score": score,
                "confidence": confidence,
                "breakdown": breakdown,
            }
        )

    results.sort(key=lambda r: r["score"], reverse=True)
    return results
