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


def unfilled_roles(state: DraftState, conn: sqlite3.Connection) -> list[str]:
    """Roles from `state.roster` (our 5 pre-assigned roster roles) that no pick-so-far has been
    best-matched to yet. Best-effort matching, not a strict declared role -- a pick doesn't
    literally announce "I am jungle," so we look up each picked champion's role-eligibility
    rows and take the eligible role with the most games_observed that is ALSO one of our
    still-tracked roster roles."""
    remaining_roles = {assignment.role for assignment in state.roster}
    for champion_id in our_picks_so_far(state):
        matched_role = _best_matching_role(conn, champion_id, remaining_roles)
        if matched_role is not None:
            remaining_roles.discard(matched_role)
    # Preserve a stable, deterministic ordering (roster order of first occurrence).
    seen: list[str] = []
    for assignment in state.roster:
        if assignment.role in remaining_roles and assignment.role not in seen:
            seen.append(assignment.role)
    return seen


def is_draft_complete(state: DraftState) -> bool:
    return state.current_slot == len(DRAFT_SEQUENCE)
