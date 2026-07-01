"""The fixed 20-slot Tournament Draft action sequence.

The LoL Wiki's Tournament Draft spec describes 17 *steps*, but three of those steps
(8, 9, 16) each grant a team 2 consecutive picks. Since this state machine's atomic
unit is one champion-action (not one wiki step), the 17-step spec expands to a fixed
20-slot table: 10 bans + 10 picks, 5 bans/5 picks per side.

This table is static, already-verified domain data (checked against the LoL Wiki) --
it is NOT re-derived here, only encoded. Every other part of the draft engine (state
machine, queries, scoring) treats this table as the single source of truth for
"whose turn is it, and what action."

`opp_pick_gap_after` answers: for a PICK slot, how many picks does the *opponent* get
before this team picks again (or before the draft ends, if there's no next same-team
pick)? This drives pick-safety scoring -- a pick made right before the opponent gets
two responses in a row (gap=2) is far riskier to "waste" on a narrow/uncounterable
champion than a pick made with no opponent response before the draft ends (gap=0).
It is intentionally left `None` for BAN slots -- gap only measures exposure of an
already-locked-in pick, and bans don't lock anything in.

There is deliberately no "opponent ban gap" field: it was considered during design
and dropped because nothing in the scoring algorithm consumes it (bans don't threaten
an already-locked-in composition the way an unanswered pick does).
"""
from __future__ import annotations

from typing import NamedTuple

BLUE = "BLUE"
RED = "RED"
BAN = "BAN"
PICK = "PICK"


class DraftSlot(NamedTuple):
    slot: int
    side: str  # "BLUE" | "RED"
    action: str  # "BAN" | "PICK"
    wiki_step: int
    opp_pick_gap_after: int | None  # None for BAN slots; int (0/1/2) for PICK slots


DRAFT_SEQUENCE: list[DraftSlot] = [
    DraftSlot(0, BLUE, BAN, 1, None),
    DraftSlot(1, RED, BAN, 2, None),
    DraftSlot(2, BLUE, BAN, 3, None),
    DraftSlot(3, RED, BAN, 4, None),
    DraftSlot(4, BLUE, BAN, 5, None),
    DraftSlot(5, RED, BAN, 6, None),
    DraftSlot(6, BLUE, PICK, 7, 2),
    DraftSlot(7, RED, PICK, 8, 0),
    DraftSlot(8, RED, PICK, 8, 2),
    DraftSlot(9, BLUE, PICK, 9, 0),
    DraftSlot(10, BLUE, PICK, 9, 1),
    DraftSlot(11, RED, PICK, 10, 0),
    DraftSlot(12, RED, BAN, 11, None),
    DraftSlot(13, BLUE, BAN, 12, None),
    DraftSlot(14, RED, BAN, 13, None),
    DraftSlot(15, BLUE, BAN, 14, None),
    DraftSlot(16, RED, PICK, 15, 2),
    DraftSlot(17, BLUE, PICK, 16, 0),
    DraftSlot(18, BLUE, PICK, 16, 1),
    DraftSlot(19, RED, PICK, 17, 0),
]

assert len(DRAFT_SEQUENCE) == 20
assert all(s.slot == i for i, s in enumerate(DRAFT_SEQUENCE))


def get_slot(slot: int) -> DraftSlot:
    """Bounds-checked accessor -- raises IndexError with a clear message for out-of-range slots
    rather than letting a bare list index error propagate."""
    if not 0 <= slot < len(DRAFT_SEQUENCE):
        raise IndexError(f"slot {slot} is out of range [0, {len(DRAFT_SEQUENCE)})")
    return DRAFT_SEQUENCE[slot]
