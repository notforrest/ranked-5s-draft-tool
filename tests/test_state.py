"""Tests for draftassistant.draft.state.DraftState: forward-only enter(), and the undo_last()
correction path."""
import pytest

from draftassistant.draft.state import DraftState, DraftValidationError, RosterAssignment


def make_roster(side: str = "BLUE") -> list[RosterAssignment]:
    roles = ["TOP", "JUNGLE", "MID", "BOTTOM", "SUPPORT"]
    return [RosterAssignment(player_id=i + 1, role=role, side=side) for i, role in enumerate(roles)]


def make_state(our_side: str = "BLUE") -> DraftState:
    return DraftState(our_side=our_side, roster=make_roster(our_side))


# ------------------------------------------------------------------
# enter()
# ------------------------------------------------------------------


def test_enter_at_slot_zero_succeeds():
    state = make_state()
    state.enter(0, champion_id=100)
    assert state.entries[0].champion_id == 100
    assert state.current_slot == 1


def test_enter_advances_current_slot_sequentially():
    state = make_state()
    for slot, champ in enumerate([1, 2, 3, 4, 5]):
        state.enter(slot, champ)
        assert state.current_slot == slot + 1


def test_enter_out_of_order_slot_too_high_raises():
    state = make_state()
    with pytest.raises(DraftValidationError):
        state.enter(1, champion_id=100)  # current_slot is 0, not 1
    # state must be untouched
    assert state.current_slot == 0
    assert state.entries[1] is None


def test_enter_out_of_order_slot_too_low_raises():
    state = make_state()
    state.enter(0, champion_id=100)
    with pytest.raises(DraftValidationError):
        state.enter(0, champion_id=200)  # slot 0 already filled, current_slot is now 1
    assert state.current_slot == 1
    assert state.entries[0].champion_id == 100  # unchanged


def test_enter_duplicate_champion_raises():
    state = make_state()
    state.enter(0, champion_id=100)
    with pytest.raises(DraftValidationError):
        state.enter(1, champion_id=100)
    # state untouched by the failed attempt
    assert state.current_slot == 1
    assert state.entries[1] is None


def test_enter_slot_out_of_range_raises():
    state = make_state()
    with pytest.raises(DraftValidationError):
        state.enter(20, champion_id=100)
    with pytest.raises(DraftValidationError):
        state.enter(-1, champion_id=100)


def test_enter_does_not_mutate_state_on_failure():
    state = make_state()
    state.enter(0, champion_id=100)
    snapshot_before = state.to_dict()
    with pytest.raises(DraftValidationError):
        state.enter(0, champion_id=999)  # wrong slot (already past it)
    assert state.to_dict() == snapshot_before


def test_full_draft_can_be_entered_without_collisions():
    state = make_state()
    for slot in range(20):
        state.enter(slot, champion_id=1000 + slot)
    assert state.current_slot == 20
    assert all(e is not None for e in state.entries)


# ------------------------------------------------------------------
# undo_last()
# ------------------------------------------------------------------


def test_undo_last_clears_the_most_recent_slot():
    state = make_state()
    state.enter(0, champion_id=100)
    state.enter(1, champion_id=200)
    state.undo_last()
    assert state.entries[1] is None
    assert state.current_slot == 1
    assert state.entries[0].champion_id == 100  # unaffected


def test_undo_last_on_empty_draft_raises():
    state = make_state()
    with pytest.raises(DraftValidationError):
        state.undo_last()
    assert state.current_slot == 0


def test_undo_last_repeated_calls_unwind_multiple_entries():
    state = make_state()
    for slot, champ in enumerate([100, 200, 300]):
        state.enter(slot, champ)
    state.undo_last()
    state.undo_last()
    assert state.current_slot == 1
    assert state.entries[0].champion_id == 100
    assert state.entries[1] is None
    assert state.entries[2] is None


def test_undo_last_then_reentering_the_same_champion_succeeds():
    """The whole point of undo -- the champion freed up by undoing must be immediately
    re-pickable (e.g. correcting a misclick), not still counted as "used" anywhere."""
    state = make_state()
    state.enter(0, champion_id=100)
    state.undo_last()
    state.enter(0, champion_id=100)  # re-enter the same champion at the freed-up slot
    assert state.entries[0].champion_id == 100
    assert state.current_slot == 1


def test_undo_last_clears_a_role_override_on_the_undone_slot():
    state = _state_with_blue_pick_at_6()
    state.set_role_override(6, "JUNGLE")
    state.undo_last()
    assert state.entries[6] is None  # override is gone along with the whole entry


def test_undo_last_does_not_mutate_state_on_failure():
    state = make_state()
    snapshot_before = state.to_dict()
    with pytest.raises(DraftValidationError):
        state.undo_last()
    assert state.to_dict() == snapshot_before


# ------------------------------------------------------------------
# set_role_override() / clear_role_override()
# ------------------------------------------------------------------
# BLUE's real PICK slots (per DRAFT_SEQUENCE) are 6, 9, 10, 17, 18; RED's are 7, 8, 11, 16, 19;
# BAN slots are 0-5 and 12-15. Unlike enter()/undo_last(), these two methods DO care about a
# slot's real action/side, so (unlike the tests above) real slot numbers matter here.


def _state_with_blue_pick_at_6() -> DraftState:
    state = make_state("BLUE")
    for slot in range(6):
        state.enter(slot, champion_id=100 + slot)
    state.enter(6, champion_id=1)  # BLUE's first real pick
    return state


def test_set_role_override_valid():
    state = _state_with_blue_pick_at_6()
    state.set_role_override(6, "JUNGLE")
    assert state.entries[6].role_override == "JUNGLE"


def test_clear_role_override_valid():
    state = _state_with_blue_pick_at_6()
    state.set_role_override(6, "JUNGLE")
    state.clear_role_override(6)
    assert state.entries[6].role_override is None


def test_clear_role_override_on_unset_override_is_a_noop():
    state = _state_with_blue_pick_at_6()
    state.clear_role_override(6)  # never set -- must not raise
    assert state.entries[6].role_override is None


def test_set_role_override_slot_out_of_range_raises():
    state = _state_with_blue_pick_at_6()
    with pytest.raises(DraftValidationError):
        state.set_role_override(20, "JUNGLE")
    with pytest.raises(DraftValidationError):
        state.set_role_override(-1, "JUNGLE")


def test_set_role_override_unfilled_slot_raises():
    state = _state_with_blue_pick_at_6()
    with pytest.raises(DraftValidationError):
        state.set_role_override(9, "JUNGLE")  # BLUE's next pick slot, not entered yet


def test_set_role_override_on_ban_slot_raises():
    state = _state_with_blue_pick_at_6()
    with pytest.raises(DraftValidationError):
        state.set_role_override(0, "JUNGLE")  # slot 0 is a BAN, not a PICK
    assert state.entries[0].role_override is None


def test_set_role_override_on_opponents_pick_raises():
    state = make_state("BLUE")
    for slot in range(8):
        state.enter(slot, champion_id=100 + slot)  # reaches slot 7, RED's pick
    with pytest.raises(DraftValidationError):
        state.set_role_override(7, "JUNGLE")  # RED's pick -- we don't drive their roster


def test_set_role_override_invalid_role_name_raises():
    state = _state_with_blue_pick_at_6()
    with pytest.raises(DraftValidationError):
        state.set_role_override(6, "MIDDLE")  # not a valid role string (should be "MID")
    assert state.entries[6].role_override is None


def test_set_role_override_does_not_mutate_state_on_failure():
    state = _state_with_blue_pick_at_6()
    snapshot_before = state.to_dict()
    with pytest.raises(DraftValidationError):
        state.set_role_override(6, "NOT_A_ROLE")
    assert state.to_dict() == snapshot_before


# ------------------------------------------------------------------
# to_dict / from_dict round-trip
# ------------------------------------------------------------------


def test_to_dict_from_dict_round_trip():
    state = make_state()
    state.enter(0, champion_id=100)
    state.enter(1, champion_id=200)

    restored = DraftState.from_dict(state.to_dict())

    assert restored.our_side == state.our_side
    assert restored.current_slot == state.current_slot
    assert [r.player_id for r in restored.roster] == [r.player_id for r in state.roster]
    assert restored.entries[0].champion_id == 100
    assert restored.entries[1].champion_id == 200
    assert restored.entries[2] is None
