"""Live-draft-facing endpoints.

HARD ARCHITECTURAL RULE: this file must NEVER import anything from `riot_client/`,
`refresh/`, or `ingest/` -- directly or transitively through a new import added here.
Only the setup/refresh router (`api/routers/refresh.py`) is allowed to call
`refresh.pre_draft_refresh.run`. This is what guarantees the live draft screen is
instant and never blocked by an expired Riot API key or a network issue mid-draft.
This file only ever touches: the DB repositories, `draft.state`, `draft.queries`,
`draft.sequence` (static data, not the Riot client), and `scoring.*`.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from draftassistant.api.deps import get_db
from draftassistant.db.repositories import draft_session_repo, roster_repo, tierlist_repo
from draftassistant.draft import queries
from draftassistant.draft.sequence import DRAFT_SEQUENCE
from draftassistant.draft.state import DraftState, DraftValidationError, RosterAssignment
from draftassistant.scoring import ban_score, champion_detail, pick_score

router = APIRouter(tags=["draft"])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _load_state(conn: sqlite3.Connection, draft_session_id: int) -> DraftState:
    row = draft_session_repo.get_draft_state(conn, draft_session_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"draft_session_id {draft_session_id} not found")
    return DraftState.from_dict(
        {
            "our_side": row["our_side"],
            "current_slot": row["current_slot"],
            "roster": _roster_from_lineup(conn, row),
            "entries": row["entries"],
        }
    )


def _roster_from_lineup(conn: sqlite3.Connection, draft_row: dict) -> list[dict]:
    """The `draft_sessions` table (see schema.sql) has no roster column of its own -- it only
    stores `session_id`, a foreign key to `game_sessions` / `session_lineup`. The roster is
    reconstructed from there on every load rather than duplicated into the entries_json
    snapshot, so a lineup fix (unlikely mid-draft, but possible before draft creation) can
    never desync from what's actually persisted."""
    session_id = draft_row.get("session_id")
    if session_id is None:
        return []
    lineup = roster_repo.get_session_lineup(conn, session_id)
    our_side = draft_row["our_side"]
    return [
        {"player_id": entry["player_id"], "role": entry["assigned_role"], "side": our_side}
        for entry in lineup
    ]


def _save_state(conn: sqlite3.Connection, draft_session_id: int, state: DraftState) -> None:
    draft_session_repo.save_draft_state(
        conn,
        draft_session_id,
        current_slot=state.current_slot,
        entries=state.to_dict()["entries"],
    )


def _serialize_entries(state: DraftState, conn: sqlite3.Connection) -> list[dict | None]:
    """Zips `state.entries` with the static per-slot side/action/wiki_step metadata from
    `sequence.DRAFT_SEQUENCE` so the frontend can render the two-column draft board without
    duplicating that table in JS -- each non-null element carries both the live entry data
    and its fixed slot metadata. Also attaches each our-side pick's resolved role (auto-computed
    or manually overridden, see queries.resolve_pick_roles) directly onto the entry, so the
    board's existing per-cell rendering loop can show a role badge with no separate lookup by
    slot number. Non-pick slots and the opponent's picks always carry `resolved_role: None`,
    since resolve_pick_roles only ever populates keys for our-side filled pick slots."""
    resolved_roles = queries.resolve_pick_roles(state, conn)
    out: list[dict | None] = []
    for slot_def, entry in zip(DRAFT_SEQUENCE, state.entries):
        base = {
            "slot": slot_def.slot,
            "wiki_step": slot_def.wiki_step,
            "side": slot_def.side,
            "action": slot_def.action,
        }
        if entry is None:
            out.append({**base, "champion_id": None, "invalidated": False, "amended_count": 0,
                        "resolved_role": None, "is_role_override": False})
        else:
            out.append(
                {
                    **base,
                    "champion_id": entry.champion_id,
                    "entered_at": entry.entered_at,
                    "amended_count": entry.amended_count,
                    "invalidated": entry.invalidated,
                    "resolved_role": resolved_roles.get(slot_def.slot),
                    "is_role_override": entry.role_override is not None,
                }
            )
    return out


def _get_suggestions(conn: sqlite3.Connection, state: DraftState) -> dict:
    """Shared by the dedicated suggestions endpoint AND internally by enter/amend, so a
    single round-trip to enter/amend a champion also returns fresh suggestions -- the
    frontend never needs to make a second call just to refresh the suggestion panel."""
    if queries.is_draft_complete(state):
        return {"draft_complete": True, "suggestions": []}

    patch = tierlist_repo.get_latest_patch(conn)
    slot_info = queries.current_slot_info(state)
    if slot_info["action"] == "PICK":
        suggestions = pick_score.rank_pick_candidates(conn, state, patch)
    else:
        suggestions = ban_score.rank_ban_candidates(conn, state, patch)
    return {"draft_complete": False, "suggestions": suggestions}


def _state_payload(conn: sqlite3.Connection, draft_session_id: int, state: DraftState,
                    *, include_suggestions: bool = True) -> dict:
    payload = {
        "draft_session_id": draft_session_id,
        "our_side": state.our_side,
        "current_slot": state.current_slot,
        "entries": _serialize_entries(state, conn),
        "current_slot_info": queries.current_slot_info(state),
    }
    if include_suggestions:
        payload.update(_get_suggestions(conn, state))
    return payload


# ---------------------------------------------------------------------------
# POST /api/draft
# ---------------------------------------------------------------------------
class CreateDraftRequest(BaseModel):
    session_id: int
    our_side: str


@router.post("/draft", status_code=201)
def create_draft(body: CreateDraftRequest, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    if body.our_side not in ("BLUE", "RED"):
        raise HTTPException(status_code=400, detail="our_side must be 'BLUE' or 'RED'")

    lineup = roster_repo.get_session_lineup(conn, body.session_id)
    if not lineup:
        raise HTTPException(
            status_code=404, detail=f"session_id {body.session_id} has no lineup (does it exist?)"
        )

    roster = [
        RosterAssignment(player_id=entry["player_id"], role=entry["assigned_role"], side=body.our_side)
        for entry in lineup
    ]
    state = DraftState(our_side=body.our_side, roster=roster)

    draft_session_id = draft_session_repo.create_draft_session(
        conn, session_id=body.session_id, our_side=body.our_side
    )
    _save_state(conn, draft_session_id, state)

    return _state_payload(conn, draft_session_id, state)


# ---------------------------------------------------------------------------
# GET /api/draft/{draft_session_id}
# ---------------------------------------------------------------------------
@router.get("/draft/{draft_session_id}")
def get_draft(draft_session_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    state = _load_state(conn, draft_session_id)
    return _state_payload(conn, draft_session_id, state)


# ---------------------------------------------------------------------------
# POST /api/draft/{draft_session_id}/enter
# ---------------------------------------------------------------------------
class EnterRequest(BaseModel):
    champion_id: int


@router.post("/draft/{draft_session_id}/enter")
def enter_pick(draft_session_id: int, body: EnterRequest,
                conn: sqlite3.Connection = Depends(get_db)) -> dict:
    state = _load_state(conn, draft_session_id)
    try:
        # Slot is implicit -- always the state's own current_slot. The frontend never
        # specifies team/action/slot; that's the entire point of this endpoint.
        state.enter(state.current_slot, body.champion_id)
    except DraftValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _save_state(conn, draft_session_id, state)
    return _state_payload(conn, draft_session_id, state)


# ---------------------------------------------------------------------------
# POST /api/draft/{draft_session_id}/amend
# ---------------------------------------------------------------------------
class AmendRequest(BaseModel):
    slot: int
    champion_id: int


@router.post("/draft/{draft_session_id}/amend")
def amend_pick(draft_session_id: int, body: AmendRequest,
                conn: sqlite3.Connection = Depends(get_db)) -> dict:
    state = _load_state(conn, draft_session_id)
    try:
        newly_invalidated = state.amend(body.slot, body.champion_id)
    except DraftValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _save_state(conn, draft_session_id, state)
    payload = _state_payload(conn, draft_session_id, state)
    # Surfaced as a top-level field (not buried in `entries`) precisely because the caller
    # is instructed to make this loud in the UI -- "this also invalidated slot X."
    payload["newly_invalidated_slots"] = newly_invalidated
    return payload


# ---------------------------------------------------------------------------
# GET /api/draft/{draft_session_id}/suggestions
# ---------------------------------------------------------------------------
@router.get("/draft/{draft_session_id}/suggestions")
def get_suggestions(draft_session_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    state = _load_state(conn, draft_session_id)
    result = _get_suggestions(conn, state)
    result["current_slot_info"] = queries.current_slot_info(state)
    return result


# ---------------------------------------------------------------------------
# POST /api/draft/{draft_session_id}/role-override
# DELETE /api/draft/{draft_session_id}/role-override/{slot}
# ---------------------------------------------------------------------------
class SetRoleOverrideRequest(BaseModel):
    slot: int
    role: str


@router.post("/draft/{draft_session_id}/role-override")
def set_role_override(draft_session_id: int, body: SetRoleOverrideRequest,
                       conn: sqlite3.Connection = Depends(get_db)) -> dict:
    state = _load_state(conn, draft_session_id)
    try:
        state.set_role_override(body.slot, body.role)
    except DraftValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _save_state(conn, draft_session_id, state)
    return _state_payload(conn, draft_session_id, state)


@router.delete("/draft/{draft_session_id}/role-override/{slot}")
def clear_role_override(draft_session_id: int, slot: int,
                         conn: sqlite3.Connection = Depends(get_db)) -> dict:
    state = _load_state(conn, draft_session_id)
    try:
        state.clear_role_override(slot)
    except DraftValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _save_state(conn, draft_session_id, state)
    return _state_payload(conn, draft_session_id, state)


# ---------------------------------------------------------------------------
# GET /api/draft/{draft_session_id}/champion-detail/{champion_id}
# ---------------------------------------------------------------------------
@router.get("/draft/{draft_session_id}/champion-detail/{champion_id}")
def get_champion_detail(draft_session_id: int, champion_id: int, role: str | None = None,
                         conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """Rich, human-readable detail for one champion against the current draft state -- feeds
    the hover-detail panel. `role` is an optional override for future use (not currently sent
    by the frontend, which relies on server-side inference matching whatever the currently
    rendered suggestion row already implies)."""
    state = _load_state(conn, draft_session_id)
    # Display-name lookup only; intentionally all active players, not scoped to this session's
    # lineup specifically -- harmless since we only ever index into it by state.roster's own
    # player_ids, which are always a subset.
    roster_lookup = {p["player_id"]: p for p in roster_repo.get_active_players(conn)}
    return champion_detail.get_champion_detail(conn, state, champion_id, roster_lookup, requested_role=role)
