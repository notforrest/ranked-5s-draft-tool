"""Tests for draft/queries.py's role-resolution logic: resolve_pick_roles (the shared
two-pass override + auto-resolve algorithm) and unfilled_roles (its thin wrapper)."""
from __future__ import annotations

from draftassistant.db.repositories import champion_repo
from draftassistant.draft.queries import resolve_pick_roles, unfilled_roles
from draftassistant.draft.state import DraftState, RosterAssignment


def _roster() -> list[RosterAssignment]:
    return [
        RosterAssignment(player_id=1, role="TOP", side="BLUE"),
        RosterAssignment(player_id=2, role="JUNGLE", side="BLUE"),
        RosterAssignment(player_id=3, role="MID", side="BLUE"),
        RosterAssignment(player_id=4, role="BOTTOM", side="BLUE"),
        RosterAssignment(player_id=5, role="SUPPORT", side="BLUE"),
    ]


def _fill_bans(state: DraftState) -> None:
    """Slots 0-5 are the 6 bans preceding BLUE's first pick at slot 6 -- fill them with dummy
    champion ids outside any test's real candidate pool."""
    for slot in range(6):
        state.enter(slot, champion_id=900 + slot)


def test_resolve_pick_roles_empty_draft(test_db_conn):
    state = DraftState(our_side="BLUE", roster=_roster())
    assert resolve_pick_roles(state, test_db_conn) == {}
    assert set(unfilled_roles(state, test_db_conn)) == {"TOP", "JUNGLE", "MID", "BOTTOM", "SUPPORT"}


def test_tryndamere_then_gragas_worked_example(test_db_conn):
    """The user's own example: Tryndamere picked first claims TOP (his best role). Gragas
    picked second would ALSO want TOP (also his best role) but it's taken, so he falls through
    to his next-best available role, JUNGLE."""
    conn = test_db_conn
    champion_repo.upsert_champion(conn, champion_id=1, champion_key="Tryndamere", name="Tryndamere",
                                   tags=[], icon_path="", ddragon_version="14.13.1")
    champion_repo.upsert_champion(conn, champion_id=2, champion_key="Gragas", name="Gragas",
                                   tags=[], icon_path="", ddragon_version="14.13.1")
    champion_repo.replace_role_eligibility(conn, "pro", [
        (1, "TOP", 1000),
        (2, "TOP", 800), (2, "JUNGLE", 500),
    ])
    conn.commit()

    state = DraftState(our_side="BLUE", roster=_roster())
    _fill_bans(state)
    state.enter(6, champion_id=1)   # Tryndamere -- BLUE's first pick
    state.enter(7, champion_id=950)  # RED pick (not ours)
    state.enter(8, champion_id=951)  # RED pick (not ours)
    state.enter(9, champion_id=2)    # Gragas -- BLUE's second pick

    resolved = resolve_pick_roles(state, conn)
    assert resolved == {6: "TOP", 9: "JUNGLE"}
    assert "TOP" not in unfilled_roles(state, conn)
    assert "JUNGLE" not in unfilled_roles(state, conn)
    assert set(unfilled_roles(state, conn)) == {"MID", "BOTTOM", "SUPPORT"}


def test_zero_eligibility_pick_resolves_to_none(test_db_conn):
    conn = test_db_conn
    champion_repo.upsert_champion(conn, champion_id=1, champion_key="Obscure", name="Obscure",
                                   tags=[], icon_path="", ddragon_version="14.13.1")
    conn.commit()

    state = DraftState(our_side="BLUE", roster=_roster())
    _fill_bans(state)
    state.enter(6, champion_id=1)  # no eligibility rows at all

    resolved = resolve_pick_roles(state, conn)
    assert resolved == {6: None}
    # An unmatched pick claims nothing -- every role remains unfilled.
    assert set(unfilled_roles(state, conn)) == {"TOP", "JUNGLE", "MID", "BOTTOM", "SUPPORT"}


def test_override_always_wins_even_over_an_earlier_slots_only_eligible_role(test_db_conn):
    """Overrides are resolved unconditionally in pass 1, BEFORE pass 2 (auto-resolution) even
    starts -- so an override always gets exactly the role it asks for, and an auto-resolved
    pick can never retain a role an override has claimed, even if that auto-resolved pick was
    entered earlier and even if it has no OTHER eligible role to fall back to. This is the
    intended "overrides always win, never bumped, never rejected" behavior, not a bug: Kha'Zix
    (JUNGLE is his only eligible role) loses his auto-claim to Warwick's override and resolves
    to None/"Unknown" instead -- exactly the state the override UI is meant to prompt a human to
    notice and fix (e.g. by giving Kha'Zix an explicit override of his own)."""
    conn = test_db_conn
    champion_repo.upsert_champion(conn, champion_id=1, champion_key="Khazix", name="Kha'Zix",
                                   tags=[], icon_path="", ddragon_version="14.13.1")
    champion_repo.upsert_champion(conn, champion_id=2, champion_key="Warwick", name="Warwick",
                                   tags=[], icon_path="", ddragon_version="14.13.1")
    champion_repo.replace_role_eligibility(conn, "pro", [(1, "JUNGLE", 900)])
    conn.commit()

    state = DraftState(our_side="BLUE", roster=_roster())
    _fill_bans(state)
    state.enter(6, champion_id=1)  # Kha'Zix -- JUNGLE is his ONLY eligible role
    state.enter(7, champion_id=950)
    state.enter(8, champion_id=951)
    state.enter(9, champion_id=2)  # Warwick -- no eligibility, override to JUNGLE
    state.set_role_override(9, "JUNGLE")

    resolved = resolve_pick_roles(state, conn)
    assert resolved == {6: None, 9: "JUNGLE"}
    assert "JUNGLE" not in unfilled_roles(state, conn)


def test_two_overrides_may_share_the_same_role(test_db_conn):
    """Two overrides naming the same role is allowed, not rejected -- a duplicate-role roster
    is a valid real outcome the human may know about even if the tool's own data doesn't
    reflect it, and enforcing uniqueness would require a single-slot-scoped setter to scan
    every other slot, which set_role_override deliberately doesn't do."""
    conn = test_db_conn
    for cid, name in ((1, "Alpha"), (2, "Beta")):
        champion_repo.upsert_champion(conn, champion_id=cid, champion_key=name, name=name,
                                       tags=[], icon_path="", ddragon_version="14.13.1")
    conn.commit()

    state = DraftState(our_side="BLUE", roster=_roster())
    _fill_bans(state)
    state.enter(6, champion_id=1)
    state.set_role_override(6, "JUNGLE")
    state.enter(7, champion_id=950)
    state.enter(8, champion_id=951)
    state.enter(9, champion_id=2)
    state.set_role_override(9, "JUNGLE")

    resolved = resolve_pick_roles(state, conn)
    assert resolved == {6: "JUNGLE", 9: "JUNGLE"}


def test_override_on_earlier_slot_forces_later_auto_pick_to_fall_through(test_db_conn):
    """An override always fully resolves in pass 1, before pass 2 (auto-resolution) examines
    anything -- true regardless of which slot came first in draft order."""
    conn = test_db_conn
    champion_repo.upsert_champion(conn, champion_id=1, champion_key="Filler", name="Filler",
                                   tags=[], icon_path="", ddragon_version="14.13.1")
    champion_repo.upsert_champion(conn, champion_id=2, champion_key="TopOnly", name="TopOnly",
                                   tags=[], icon_path="", ddragon_version="14.13.1")
    champion_repo.replace_role_eligibility(conn, "pro", [(2, "TOP", 700)])
    conn.commit()

    state = DraftState(our_side="BLUE", roster=_roster())
    _fill_bans(state)
    state.enter(6, champion_id=1)   # Filler -- will be overridden to TOP
    state.set_role_override(6, "TOP")
    state.enter(7, champion_id=950)
    state.enter(8, champion_id=951)
    state.enter(9, champion_id=2)   # TopOnly's only eligible role, TOP, is already claimed

    resolved = resolve_pick_roles(state, conn)
    assert resolved[6] == "TOP"
    assert resolved[9] is None  # falls through -- TOP was taken by the override, no other role fits


def test_to_dict_from_dict_round_trip_with_override(test_db_conn):
    state = DraftState(our_side="BLUE", roster=_roster())
    _fill_bans(state)
    state.enter(6, champion_id=1)
    state.set_role_override(6, "SUPPORT")

    reloaded = DraftState.from_dict(state.to_dict())
    assert reloaded.entries[6].role_override == "SUPPORT"


def test_from_dict_backward_compatible_with_missing_role_override_key(test_db_conn):
    """A pre-existing persisted draft (from before role_override existed) has no such key in
    its entries at all -- must load with role_override=None, not KeyError."""
    state = DraftState(our_side="BLUE", roster=_roster())
    _fill_bans(state)
    state.enter(6, champion_id=1)
    payload = state.to_dict()
    del payload["entries"][6]["role_override"]  # simulate an old persisted blob

    reloaded = DraftState.from_dict(payload)
    assert reloaded.entries[6].role_override is None
