"""Verifies draftassistant.draft.sequence.DRAFT_SEQUENCE exactly matches the already-verified
20-slot table (checked against the LoL Wiki's Tournament Draft spec) and the precomputed
opp_pick_gap_after values, both given in the design plan."""
from draftassistant.draft.sequence import BAN, BLUE, DRAFT_SEQUENCE, PICK, RED, get_slot

# side, action, wiki_step per slot, exactly as specified.
EXPECTED_TABLE: dict[int, tuple[str, str, int]] = {
    0: (BLUE, BAN, 1),
    1: (RED, BAN, 2),
    2: (BLUE, BAN, 3),
    3: (RED, BAN, 4),
    4: (BLUE, BAN, 5),
    5: (RED, BAN, 6),
    6: (BLUE, PICK, 7),
    7: (RED, PICK, 8),
    8: (RED, PICK, 8),
    9: (BLUE, PICK, 9),
    10: (BLUE, PICK, 9),
    11: (RED, PICK, 10),
    12: (RED, BAN, 11),
    13: (BLUE, BAN, 12),
    14: (RED, BAN, 13),
    15: (BLUE, BAN, 14),
    16: (RED, PICK, 15),
    17: (BLUE, PICK, 16),
    18: (BLUE, PICK, 16),
    19: (RED, PICK, 17),
}

# opp_pick_gap_after per PICK slot, exactly as specified.
EXPECTED_GAPS: dict[int, int] = {
    6: 2,
    7: 0,
    8: 2,
    9: 0,
    10: 1,
    11: 0,
    16: 2,
    17: 0,
    18: 1,
    19: 0,
}

BAN_SLOTS = {slot for slot, (_, action, _) in EXPECTED_TABLE.items() if action == BAN}
PICK_SLOTS = {slot for slot, (_, action, _) in EXPECTED_TABLE.items() if action == PICK}


def test_total_slot_count():
    assert len(DRAFT_SEQUENCE) == 20


def test_ten_bans_and_ten_picks():
    bans = [s for s in DRAFT_SEQUENCE if s.action == BAN]
    picks = [s for s in DRAFT_SEQUENCE if s.action == PICK]
    assert len(bans) == 10
    assert len(picks) == 10


def test_five_bans_and_five_picks_per_side():
    for side in (BLUE, RED):
        side_bans = [s for s in DRAFT_SEQUENCE if s.side == side and s.action == BAN]
        side_picks = [s for s in DRAFT_SEQUENCE if s.side == side and s.action == PICK]
        assert len(side_bans) == 5, f"{side} should have exactly 5 bans"
        assert len(side_picks) == 5, f"{side} should have exactly 5 picks"


def test_every_slot_matches_side_action_wiki_step():
    for slot, (expected_side, expected_action, expected_wiki_step) in EXPECTED_TABLE.items():
        actual = get_slot(slot)
        assert actual.slot == slot
        assert actual.side == expected_side, f"slot {slot}: side mismatch"
        assert actual.action == expected_action, f"slot {slot}: action mismatch"
        assert actual.wiki_step == expected_wiki_step, f"slot {slot}: wiki_step mismatch"


def test_ban_slots_have_no_gap():
    for slot in BAN_SLOTS:
        assert get_slot(slot).opp_pick_gap_after is None, f"BAN slot {slot} should have gap=None"


def test_every_pick_slot_gap_matches_precomputed_table():
    for slot, expected_gap in EXPECTED_GAPS.items():
        actual_gap = get_slot(slot).opp_pick_gap_after
        assert actual_gap == expected_gap, f"slot {slot}: expected gap {expected_gap}, got {actual_gap}"


def test_all_pick_slots_covered_by_gap_table():
    assert set(EXPECTED_GAPS.keys()) == PICK_SLOTS


def test_slots_are_contiguous_and_index_aligned():
    for i, slot_def in enumerate(DRAFT_SEQUENCE):
        assert slot_def.slot == i


def test_no_opponent_ban_gap_field_exists():
    """Explicitly documents the deliberate omission: DraftSlot only has 5 fields, none of
    which is an 'opponent ban gap' -- it was considered and dropped because nothing in the
    scoring design consumes it."""
    slot_fields = DRAFT_SEQUENCE[0]._fields
    assert slot_fields == ("slot", "side", "action", "wiki_step", "opp_pick_gap_after")
