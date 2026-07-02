"""Pick-suggestion scoring.

Scope note: this is a single-ply heuristic scorer, re-run fresh after every `enter`/`undo`
against the DraftState. It is NOT a game-theoretic search over the remaining draft tree --
it never looks ahead at what the opponent might do beyond the static `pick_safety` exposure
signal below. A future minimax/MCTS layer could plug this in as its leaf evaluator, but that
kind of multi-ply search is explicitly out of scope for this module.

Core mechanism: "available-case weighted renormalization." Every signal that feeds a score is
normalized to [0,1] and may legitimately be `None` when the underlying data doesn't exist (a
champion with zero personal games, absent from the curated tier list, no synergy row yet,
etc). A missing signal is *excluded* entirely from both the numerator and the weight total --
never defaulted to 0 or 0.5 -- so a data-thin champion still ranks sensibly on whatever
signals *are* available, and the accompanying `confidence` value (fraction of total weight
actually backed by real data) tells the caller how much to trust that ranking rather than
hiding the gap.
"""
from __future__ import annotations

import math
import sqlite3

from draftassistant.db.repositories import (
    champion_repo,
    mastery_repo,
    personal_stats_repo,
    synergy_repo,
    tierlist_repo,
)
from draftassistant.draft import queries
from draftassistant.draft.sequence import DRAFT_SEQUENCE
from draftassistant.draft.state import DraftState

DEFAULT_PICK_WEIGHTS: dict[str, float] = {
    "global_win_rate": 0.20,
    "global_pick_rate": 0.05,
    "personal_mastery": 0.15,
    "personal_win_rate": 0.15,
    "pro_synergy": 0.10,
    "roster_synergy": 0.10,
    "role_need": 0.15,
    "pick_safety": 0.10,
}
assert abs(sum(DEFAULT_PICK_WEIGHTS.values()) - 1.0) < 1e-9


# ======================================================================================
# Shared mechanism (imported by scoring/ban_score.py too -- kept here, not duplicated)
# ======================================================================================


def score_with_confidence(
    signals: dict[str, float | None], weights: dict[str, float]
) -> tuple[float, float]:
    """Available-case weighted renormalization.

    `signals`: {name: value_or_None}, each value (when not None) expected in [0,1].
    `weights`: {name: base_weight}; the *full* weight set the caller cares about -- callers
    should pass the same fixed weight dict every time (e.g. DEFAULT_PICK_WEIGHTS) so that
    `confidence` means the same thing call over call: what fraction of total possible weight
    was actually backed by real data this time.

    Returns (score, confidence):
      - score: weighted average of the AVAILABLE signals only (missing ones excluded from
        both numerator and denominator -- never defaulted to 0 or 0.5).
      - confidence: denom / sum(weights.values()) -- how much of the total weight budget was
        actually backed by data.
      - If literally no signal has a value (denom == 0): returns (0.5, 0.0) as an absolute
        last resort (no information at all -- score is a neutral midpoint, confidence is zero).
    """
    numerator = 0.0
    denom = 0.0
    for name, value in signals.items():
        if value is None:
            continue
        weight = weights[name]
        numerator += weight * value
        denom += weight

    if denom == 0:
        return 0.5, 0.0

    total_weight = sum(weights.values())
    return numerator / denom, denom / total_weight


def _min_max_scale(value: float, values: list[float]) -> float | None:
    """Scale `value` into [0,1] against the min/max of `values`. None if `values` is empty or
    all values are identical (no meaningful range to scale against -- in that degenerate case
    every candidate is equally "best," which the caller should treat as a missing signal, not
    force to an arbitrary constant)."""
    if not values:
        return None
    lo, hi = min(values), max(values)
    if hi == lo:
        return None
    return (value - lo) / (hi - lo)


def _average_synergy(
    conn: sqlite3.Connection, table: str, allies: list[int], candidate: int
) -> float | None:
    """Games-weighted average win rate of `candidate` alongside each already-picked `allies`
    champion, using pairwise rows from `table` (synergy_pro | synergy_roster). Skips ally pairs
    with no data row; returns None if there are no allies at all yet, or if none of the allies
    that exist have a data row for this pair (nothing to synergize with / no data to synergize
    on, either way excluded rather than defaulted to 0)."""
    if not allies:
        return None
    total_games = 0
    total_wins = 0.0
    for ally in allies:
        row = synergy_repo.get_synergy(conn, table, ally, candidate)
        if row is None or row["games_together"] <= 0:
            continue
        total_games += row["games_together"]
        total_wins += row["wins_together"]
    if total_games == 0:
        return None
    return total_wins / total_games


# ======================================================================================
# Tier-list-derived signals (shared shape used by both pick and ban scoring)
# ======================================================================================


def _role_tier_signals(
    conn: sqlite3.Connection, champion_id: int, role: str, patch: str
) -> tuple[float | None, float | None]:
    """(global_win_rate, global_pick_rate) for `champion_id` in `role` on `patch`, each
    min-max scaled within that role+patch's full tier list. win_rate scales directly;
    pick_rate is log-scaled first (pick rates are heavily right-skewed -- a handful of S-tier
    champions get picked far more than the rest, so a plain linear min-max would compress
    everything else near 0) then min-max scaled. None if the champion has no tier entry for
    this role+patch, or if the underlying stat itself is None/absent in the entry, or if the
    role's tier list doesn't have enough spread to scale against (see _min_max_scale)."""
    entry = tierlist_repo.get_tier_entry(conn, champion_id, role, patch)
    if entry is None:
        return None, None

    role_list = tierlist_repo.get_tier_list_for_patch(conn, patch)
    role_list = [r for r in role_list if r["role"] == role]

    win_rate_signal = None
    if entry["win_rate"] is not None:
        win_rates = [r["win_rate"] for r in role_list if r["win_rate"] is not None]
        win_rate_signal = _min_max_scale(entry["win_rate"], win_rates)

    pick_rate_signal = None
    if entry["pick_rate"] is not None and entry["pick_rate"] > 0:
        log_pick_rates = [
            math.log(r["pick_rate"]) for r in role_list if r["pick_rate"] is not None and r["pick_rate"] > 0
        ]
        pick_rate_signal = _min_max_scale(math.log(entry["pick_rate"]), log_pick_rates)

    return win_rate_signal, pick_rate_signal


def _best_tier_role(conn: sqlite3.Connection, champion_id: int, patch: str) -> str | None:
    """The candidate's single best (highest win_rate) role entry in the tier list for `patch`,
    across ALL roles -- used by ban scoring, which isn't role-specific the way pick scoring is.
    Simplification noted here per spec: a champion's ban value is represented by its strongest
    role rather than a full multi-role blend. None if the champion has no tier entries at all
    for this patch."""
    all_entries = [
        e
        for e in tierlist_repo.get_tier_list_for_patch(conn, patch)
        if e["champion_id"] == champion_id and e["win_rate"] is not None
    ]
    if not all_entries:
        return None
    return max(all_entries, key=lambda e: e["win_rate"])["role"]


# ======================================================================================
# Per-signal helpers specific to pick scoring
# ======================================================================================


def _player_for_role(state: DraftState, role: str) -> int | None:
    for assignment in state.roster:
        if assignment.role == role:
            return assignment.player_id
    return None


def _relevant_role_for_candidate(
    conn: sqlite3.Connection, champion_id: int, unfilled: list[str]
) -> str | None:
    """The role this candidate is being evaluated "as" for this pick: the eligible role with
    the most games_observed that is also in `unfilled` (mirrors queries._best_matching_role).
    Falls back to the champion's single best-games-observed eligible role of any kind if
    `unfilled` is empty (last pick, no role filter applies) or doesn't intersect its
    eligibility at all. None only if the champion has zero eligibility rows whatsoever."""
    eligibility = champion_repo.get_role_eligibility(conn, champion_id)
    if not eligibility:
        return None
    if unfilled:
        candidates = {role: info for role, info in eligibility.items() if role in unfilled}
        if candidates:
            return max(candidates, key=lambda r: candidates[r]["games_observed"])
    return max(eligibility, key=lambda r: eligibility[r]["games_observed"])


def _personal_mastery_signal(
    conn: sqlite3.Connection, player_id: int | None, champion_id: int
) -> float | None:
    """Candidate's mastery points scaled against `player_id`'s OWN mastery distribution (not a
    global scale) -- so "high mastery" means relatively high FOR THAT PLAYER, since raw point
    totals vary wildly by how long someone's played. None if we don't know which player is
    relevant, the player has no mastery entry for this champion, or the player's distribution
    has no spread to scale against."""
    if player_id is None:
        return None
    entry = mastery_repo.get_mastery(conn, player_id, champion_id)
    if entry is None:
        return None
    distribution = mastery_repo.get_player_mastery_distribution(conn, player_id)
    points = [d["champion_points"] for d in distribution]
    return _min_max_scale(entry["champion_points"], points)


def _personal_win_rate_signal(
    conn: sqlite3.Connection, player_id: int | None, champion_id: int
) -> float | None:
    """Bayesian-shrunk personal win rate: (wins + 5) / (games + 10). This constant already
    centers appropriately around 0.5 (a 0-game champion shrinks to exactly 0.5), so -- unlike
    every other signal here -- this one is used directly after clamping to [0,1], with NO
    additional min-max scaling against other champions. None only if the player has literally
    never played this champion (no personal_champion_stats row at all), so there's nothing to
    shrink."""
    if player_id is None:
        return None
    stats = personal_stats_repo.get_personal_stats(conn, player_id, champion_id)
    if stats is None:
        return None
    shrunk = (stats["wins"] + 5) / (stats["games"] + 10)
    return max(0.0, min(1.0, shrunk))


def _role_need_signal(unfilled: list[str], relevant_role: str | None) -> float | None:
    """1.0 if the candidate's best-matching role is our top unfilled need, 0.5 if it has some
    OTHER eligible role that's also in `unfilled` (flex fit). Since candidates reaching this
    function have already passed the role-eligibility-overlaps-unfilled-roles gate in
    rank_pick_candidates (or unfilled is empty, meaning no gate applied at all), this should
    always resolve to 1.0 or 0.5 for anything actually scored -- None is reserved for the edge
    case of a champion with zero eligibility rows in the DB at all (degrades gracefully rather
    than crashing)."""
    if relevant_role is None:
        return None
    if not unfilled:
        # Last pick, or role wasn't gated at all -- treat as fully meeting whatever's asked.
        return 1.0
    return 1.0 if relevant_role == unfilled[0] else 0.5


def _flexibility_signal(conn: sqlite3.Connection, champion_id: int, patch: str) -> float:
    """Sub-composite feeding pick_safety: a multi-role-tag bonus (1.0 if this champion has 2+
    eligible roles at all, else 0.0) blended with global_pick_rate reused as a generic-safety
    proxy (a more commonly-picked-everywhere champion is a safer "any role, any matchup"
    fallback). Sub-weights: 0.5 multi-role bonus / 0.5 pick-rate proxy, renormalized via
    score_with_confidence if pick_rate is unavailable (falls back to the multi-role bonus
    alone; if that's also somehow unresolvable this degrades to the (0.5, 0.0) last resort)."""
    eligibility = champion_repo.get_role_eligibility(conn, champion_id)
    multi_role_bonus = 1.0 if len(eligibility) >= 2 else 0.0

    # Reuse global_pick_rate as a generic-safety proxy -- take the candidate's best tier-list
    # role for this patch (bans-style lookup) since flexibility isn't evaluated for one
    # specific role.
    pick_rate_proxy = None
    best_role = _best_tier_role(conn, champion_id, patch)
    if best_role is not None:
        _, pick_rate_proxy = _role_tier_signals(conn, champion_id, best_role, patch)

    sub_weights = {"multi_role_bonus": 0.5, "pick_rate_proxy": 0.5}
    score, _confidence = score_with_confidence(
        {"multi_role_bonus": multi_role_bonus, "pick_rate_proxy": pick_rate_proxy}, sub_weights
    )
    return score


def _pick_safety_signal(conn: sqlite3.Connection, champion_id: int, patch: str, opp_pick_gap_after: int | None) -> float | None:
    """flexibility ** 1.5 at gap=2 (sharpest penalty -- highest-risk slots), flexibility at
    gap=1, flexibility * 0.5 + 0.5 at gap=0 (barely penalized -- floor raised toward 1.0 since
    there's minimal opponent-response exposure). None if `opp_pick_gap_after` isn't available
    (e.g. queried outside of an actual PICK slot) -- callers should only invoke this at a real
    PICK slot's gap."""
    if opp_pick_gap_after is None:
        return None
    flexibility = _flexibility_signal(conn, champion_id, patch)
    if opp_pick_gap_after == 2:
        return flexibility ** 1.5
    if opp_pick_gap_after == 1:
        return flexibility
    return flexibility * 0.5 + 0.5


# ======================================================================================
# Public entry point
# ======================================================================================


def rank_pick_candidates(
    conn: sqlite3.Connection,
    state: DraftState,
    patch: str,
    all_champion_ids: list[int] | None = None,
    weights: dict[str, float] | None = None,
) -> list[dict]:
    """Rank every legal, role-relevant candidate champion for the current pick.

    `all_champion_ids`: universe of champions to consider; defaults to every champion_id
    known to the champions table (via champion_repo.get_all_champions) if not supplied.
    """
    weights = weights or DEFAULT_PICK_WEIGHTS
    if all_champion_ids is None:
        all_champion_ids = [c["champion_id"] for c in champion_repo.get_all_champions(conn)]

    unfilled = queries.unfilled_roles(state, conn)
    available = queries.available_champions(state, all_champion_ids)
    slot_def = DRAFT_SEQUENCE[state.current_slot] if state.current_slot < len(DRAFT_SEQUENCE) else None
    opp_pick_gap_after = slot_def.opp_pick_gap_after if slot_def is not None else None

    allies = queries.our_picks_so_far(state)

    results: list[dict] = []
    for champion_id in available:
        eligibility = champion_repo.get_role_eligibility(conn, champion_id)

        if unfilled:
            # Role-gate: candidate must have some eligible role overlapping our unfilled roles.
            if not (set(eligibility.keys()) & set(unfilled)):
                continue

        relevant_role = _relevant_role_for_candidate(conn, champion_id, unfilled)

        global_win_rate = None
        global_pick_rate = None
        if relevant_role is not None:
            global_win_rate, global_pick_rate = _role_tier_signals(conn, champion_id, relevant_role, patch)

        player_id = _player_for_role(state, relevant_role) if relevant_role is not None else None
        personal_mastery = _personal_mastery_signal(conn, player_id, champion_id)
        personal_win_rate = _personal_win_rate_signal(conn, player_id, champion_id)

        pro_synergy = _average_synergy(conn, "synergy_pro", allies, champion_id)
        roster_synergy = _average_synergy(conn, "synergy_roster", allies, champion_id)

        role_need = _role_need_signal(unfilled, relevant_role)

        pick_safety = _pick_safety_signal(conn, champion_id, patch, opp_pick_gap_after)

        breakdown = {
            "global_win_rate": global_win_rate,
            "global_pick_rate": global_pick_rate,
            "personal_mastery": personal_mastery,
            "personal_win_rate": personal_win_rate,
            "pro_synergy": pro_synergy,
            "roster_synergy": roster_synergy,
            "role_need": role_need,
            "pick_safety": pick_safety,
        }
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
