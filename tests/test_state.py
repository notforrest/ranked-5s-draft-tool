"""Tests for draftassistant.draft.state.DraftState: forward-only enter(), and the amend()
correction path including downstream-invalidation flagging."""
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
# amend()
# ------------------------------------------------------------------


def test_amend_on_filled_slot_succeeds():
    state = make_state()
    state.enter(0, champion_id=100)
    state.enter(1, champion_id=200)
    flagged = state.amend(0, champion_id=150)
    assert state.entries[0].champion_id == 150
    assert state.entries[0].amended_count == 1
    assert flagged == []


def test_amend_on_unfilled_slot_raises():
    state = make_state()
    with pytest.raises(DraftValidationError):
        state.amend(0, champion_id=100)


def test_amend_self_reassignment_is_a_noop_not_an_error():
    state = make_state()
    state.enter(0, champion_id=100)
    flagged = state.amend(0, champion_id=100)  # same champion -- trivial, must be allowed
    assert flagged == []
    assert state.entries[0].champion_id == 100
    assert state.entries[0].amended_count == 1  # still counts as an amend call


def test_amend_collision_with_earlier_filled_slot_raises():
    """Colliding with a slot BEFORE the one being amended is a hard error -- that earlier
    champion was locked in first and is presumptively correct."""
    state = make_state()
    state.enter(0, champion_id=100)
    state.enter(1, champion_id=200)
    with pytest.raises(DraftValidationError):
        state.amend(1, champion_id=100)  # collides with slot 0, which is BEFORE slot 1
    # slot 1 must remain unchanged after the rejected amend
    assert state.entries[1].champion_id == 200
    assert state.entries[1].amended_count == 0


def test_amend_collision_with_later_filled_slot_does_not_raise():
    """Colliding with a slot AFTER the one being amended is explicitly NOT an error -- the
    amendment always succeeds and the later slot gets flagged instead (see
    test_amend_flags_downstream_collision_instead_of_deleting for the flagging behavior)."""
    state = make_state()
    state.enter(0, champion_id=100)
    state.enter(1, champion_id=200)
    flagged = state.amend(0, champion_id=200)  # collides with slot 1, which is AFTER slot 0
    assert state.entries[0].champion_id == 200
    assert flagged == [1]
    assert state.entries[1].invalidated is True


def test_amend_flags_downstream_collision_instead_of_deleting():
    state = make_state()
    for slot, champ in enumerate([100, 200, 300, 400, 500, 600]):
        state.enter(slot, champ)
    # Correct slot 0 to a new champion that happens to equal slot 4's current champion (500).
    flagged = state.amend(0, champion_id=500)
    assert flagged == [4]
    # slot 0 updated
    assert state.entries[0].champion_id == 500
    # slot 4 NOT deleted -- still present, but flagged invalidated
    assert state.entries[4] is not None
    assert state.entries[4].champion_id == 500
    assert state.entries[4].invalidated is True


def test_amend_flags_multiple_downstream_collisions():
    """If (pathologically) more than one later slot's champion now collides, all get flagged."""
    state = make_state()
    for slot, champ in enumerate([100, 200, 300, 400, 500]):
        state.enter(slot, champ)
    # Manually force a duplicate downstream scenario is not directly possible via enter() since
    # enter() itself forbids duplicates -- so we only ever get exactly one pre-existing champion
    # value to collide with per amend. Confirm the single-collision case flags exactly that slot
    # and no others.
    flagged = state.amend(0, champion_id=300)  # collides with slot 2
    assert flagged == [2]
    for slot in (1, 3, 4):
        assert state.entries[slot].invalidated is False


def test_amend_does_not_flag_slots_before_the_amended_slot():
    state = make_state()
    for slot, champ in enumerate([100, 200, 300, 400, 500]):
        state.enter(slot, champ)
    flagged = state.amend(3, champion_id=999)
    assert flagged == []
    for slot in range(3):
        assert state.entries[slot].invalidated is False


def test_amend_increments_amended_count_each_call():
    state = make_state()
    state.enter(0, champion_id=100)
    state.amend(0, champion_id=150)
    state.amend(0, champion_id=175)
    assert state.entries[0].amended_count == 2
    assert state.entries[0].champion_id == 175


def test_amend_resets_invalidated_flag_on_the_amended_slot_itself():
    """If slot X was previously flagged invalidated by an earlier amend elsewhere, and the
    driver now fixes slot X directly via its own amend(), it should no longer read as
    invalidated -- it's a fresh, intentional entry."""
    state = make_state()
    for slot, champ in enumerate([100, 200, 300, 400, 500]):
        state.enter(slot, champ)
    state.amend(0, champion_id=300)  # flags slot 2 as invalidated
    assert state.entries[2].invalidated is True
    state.amend(2, champion_id=777)  # driver now fixes slot 2 itself
    assert state.entries[2].invalidated is False
    assert state.entries[2].champion_id == 777


# ------------------------------------------------------------------
# to_dict / from_dict round-trip
# ------------------------------------------------------------------


def test_to_dict_from_dict_round_trip():
    state = make_state()
    state.enter(0, champion_id=100)
    state.enter(1, champion_id=200)
    state.amend(0, champion_id=150)

    restored = DraftState.from_dict(state.to_dict())

    assert restored.our_side == state.our_side
    assert restored.current_slot == state.current_slot
    assert [r.player_id for r in restored.roster] == [r.player_id for r in state.roster]
    assert restored.entries[0].champion_id == 150
    assert restored.entries[0].amended_count == 1
    assert restored.entries[1].champion_id == 200
    assert restored.entries[2] is None
