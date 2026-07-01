"""Free functions that read a `DraftState` (and, where a role lookup is needed, the DB
connection) without mutating anything. These are the primitives the scoring layer
(`scoring/pick_score.py`, `scoring/ban_score.py`) and the API layer build on top of.
"""
from __future__ import annotations

import sqlite3

from draftassistant.db.repositories import champion_repo
from draftassistant.draft.sequence import BAN, DRAFT_SEQUENCE, PICK
from draftassistant.draft.state import DraftState


def current_slot_info(state: DraftState) -> dict:
    """Info about `state.current_slot`, or a draft-complete indicator once all 20 slots are filled."""
    if is_draft_complete(state):
        return {
            "slot": None,
            "wiki_step": None,
            "side": None,
            "action": None,
            "is_our_turn": False,
            "opp_pick_gap_after": None,
            "is_draft_complete": True,
        }
    slot_def = DRAFT_SEQUENCE[state.current_slot]
    return {
        "slot": slot_def.slot,
        "wiki_step": slot_def.wiki_step,
        "side": slot_def.side,
        "action": slot_def.action,
        "is_our_turn": slot_def.side == state.our_side,
        "opp_pick_gap_after": slot_def.opp_pick_gap_after,
        "is_draft_complete": False,
    }


def _champion_ids_for(state: DraftState, *, action: str, side: str) -> list[int]:
    """champion_ids, in slot order, for filled (non-None) entries matching `action`/`side`.
    Invalidated entries are still included -- they were still a real action taken in the
    actual draft, only flagged for the UI to prompt a follow-up correction, not excluded
    from "what happened so far.\""""
    out: list[int] = []
    for slot_def in DRAFT_SEQUENCE:
        if slot_def.action != action or slot_def.side != side:
            continue
        entry = state.entries[slot_def.slot]
        if entry is not None:
            out.append(entry.champion_id)
    return out


def our_picks_so_far(state: DraftState) -> list[int]:
    return _champion_ids_for(state, action=PICK, side=state.our_side)


def their_side(state: DraftState) -> str:
    return "RED" if state.our_side == "BLUE" else "BLUE"


def their_picks_so_far(state: DraftState) -> list[int]:
    return _champion_ids_for(state, action=PICK, side=their_side(state))


def our_bans_so_far(state: DraftState) -> list[int]:
    return _champion_ids_for(state, action=BAN, side=state.our_side)


def their_bans_so_far(state: DraftState) -> list[int]:
    return _champion_ids_for(state, action=BAN, side=their_side(state))


def available_champions(state: DraftState, all_champion_ids: list[int]) -> list[int]:
    """`all_champion_ids` minus every champion currently occupying any (non-None) slot --
    including slots flagged `invalidated`. The champion identity was still "used" from a
    legality standpoint (enter()/amend() would reject re-picking it) even though the entry
    is flagged for the driver to review, so it must stay excluded from availability."""
    used = {entry.champion_id for entry in state.entries if entry is not None}
    return [cid for cid in all_champion_ids if cid not in used]


def _best_matching_role(conn: sqlite3.Connection, champion_id: int, candidate_roles: set[str]) -> str | None:
    """Among this champion's eligible roles that are also in `candidate_roles`, return the one
    with the most `games_observed`. None if no eligible role intersects `candidate_roles`."""
    eligibility = champion_repo.get_role_eligibility(conn, champion_id)
    best_role: str | None = None
    best_games = -1
    for role, info in eligibility.items():
        if role not in candidate_roles:
            continue
        if info["games_observed"] > best_games:
            best_games = info["games_observed"]
            best_role = role
    return best_role


def resolve_pick_roles(state: DraftState, conn: sqlite3.Connection) -> dict[int, str | None]:
    """{slot: role_or_None} for every FILLED pick slot on OUR side. A slot that hasn't been
    picked yet is simply absent from the dict -- distinct from a filled pick that resolved to
    None ("no matching role found"), which is the state the manual role-override UI targets.

    Two-pass resolution so a manual override (draft/state.py's DraftEntry.role_override) always
    wins, is never recomputed, and never gets silently bumped by another pick's own resolution:

      Pass 1 (overrides): every filled our-side pick slot with a role_override set resolves to
        that value UNCONDITIONALLY, and that role is removed from the pool available to
        auto-resolution -- regardless of slot order, regardless of whether another pick would
        have "more naturally" claimed it. Two overrides naming the SAME role is allowed, not
        rejected here (a duplicate-role roster is a valid real outcome the human may know about
        even if the tool's own data doesn't reflect it; enforcing uniqueness would require this
        single-slot-scoped resolution to reach across every other slot, which belongs in
        DraftState.set_role_override if ever wanted, not here).
      Pass 2 (auto-resolve the rest): every filled our-side pick WITHOUT an override resolves via
        the existing greedy best-match algorithm, walked in slot order, against whatever roles
        pass 1 didn't already remove. An auto-resolved pick can never reclaim a role an override
        has claimed, because pass 1 always fully completes before pass 2 examines its first
        candidate -- this is what makes "a later auto-assigned pick correctly sees an earlier
        override as unavailable" (and vice versa) true unconditionally, not just when the
        override happens to come first in slot order.
    """
    resolved: dict[int, str | None] = {}
    overridden_slots: set[int] = set()
    remaining_roles = {assignment.role for assignment in state.roster}

    for slot_def in DRAFT_SEQUENCE:  # Pass 1: overrides
        if slot_def.action != PICK or slot_def.side != state.our_side:
            continue
        entry = state.entries[slot_def.slot]
        if entry is None or entry.role_override is None:
            continue
        resolved[slot_def.slot] = entry.role_override
        overridden_slots.add(slot_def.slot)
        remaining_roles.discard(entry.role_override)

    for slot_def in DRAFT_SEQUENCE:  # Pass 2: auto-resolve everything else
        if slot_def.action != PICK or slot_def.side != state.our_side:
            continue
        if slot_def.slot in overridden_slots:
            continue
        entry = state.entries[slot_def.slot]
        if entry is None:
            continue
        matched_role = _best_matching_role(conn, entry.champion_id, remaining_roles)
        resolved[slot_def.slot] = matched_role
        if matched_role is not None:
            remaining_roles.discard(matched_role)

    return resolved


def unfilled_roles(state: DraftState, conn: sqlite3.Connection) -> list[str]:
    """Roles from `state.roster` (our 5 pre-assigned roster roles) that no pick-so-far has
    claimed yet (via auto-resolution or a manual override) -- thin wrapper around
    `resolve_pick_roles`, so there is exactly one place the actual claiming logic lives."""
    claimed = {role for role in resolve_pick_roles(state, conn).values() if role is not None}
    remaining_roles = {assignment.role for assignment in state.roster} - claimed
    # Preserve a stable, deterministic ordering (roster order of first occurrence).
    seen: list[str] = []
    for assignment in state.roster:
        if assignment.role in remaining_roles and assignment.role not in seen:
            seen.append(assignment.role)
    return seen


def is_draft_complete(state: DraftState) -> bool:
    return state.current_slot == len(DRAFT_SEQUENCE)
