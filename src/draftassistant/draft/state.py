"""The live draft state machine.

`DraftState` tracks exactly what has happened in a real Tournament Draft, slot by slot,
against the fixed `sequence.DRAFT_SEQUENCE` table. It is deliberately "dumb" about
role-fill / strategy -- it only enforces the mechanical legality rules of the real
client (forward-only entry, no duplicate champions), and never blocks an entry just
because it doesn't match some "expected" role plan. Real drafts flex, get forced by
the enemy, or get misclicked and corrected -- this state machine's job is to
faithfully transcribe what actually happened, not to enforce what "should" happen.
Role-need is purely a signal consumed later by scoring (see scoring/pick_score.py),
never a gate here.
"""
from __future__ import annotations

from dataclasses import dataclass

from draftassistant.draft.sequence import DRAFT_SEQUENCE


class DraftValidationError(Exception):
    """Raised when a caller attempts an illegal enter()/amend() against the current state."""


@dataclass
class DraftEntry:
    slot: int
    champion_id: int
    entered_at: str  # ISO-8601 timestamp string, caller/UI-supplied precision
    amended_count: int = 0
    invalidated: bool = False  # flagged (not deleted) when a later amend() creates a collision


@dataclass
class RosterAssignment:
    player_id: int
    role: str  # TOP|JUNGLE|MID|BOTTOM|SUPPORT
    side: str  # "BLUE" | "RED" -- this player's own team; must equal DraftState.our_side for all 5


class DraftState:
    def __init__(self, our_side: str, roster: list[RosterAssignment]):
        if our_side not in ("BLUE", "RED"):
            raise DraftValidationError(f"our_side must be 'BLUE' or 'RED', got {our_side!r}")
        self.our_side = our_side
        self.roster = roster
        self.current_slot = 0
        self.entries: list[DraftEntry | None] = [None] * len(DRAFT_SEQUENCE)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def enter(self, slot: int, champion_id: int, entered_at: str | None = None) -> None:
        """Forward-only entry: `slot` must be exactly `self.current_slot`. No type-ahead, no
        skipping -- the real draft is strictly sequential and suggestions are only meaningful
        against exactly-current state. Raises DraftValidationError and leaves state untouched
        on any violation."""
        if not 0 <= slot < len(DRAFT_SEQUENCE):
            raise DraftValidationError(f"slot {slot} is out of range [0, {len(DRAFT_SEQUENCE)})")
        if slot != self.current_slot:
            raise DraftValidationError(
                f"slot {slot} is not the current slot ({self.current_slot}); "
                f"entries must be made in forward order"
            )
        if self._champion_used_anywhere(champion_id, exclude_slot=None):
            raise DraftValidationError(
                f"champion_id {champion_id} has already been entered at another slot"
            )

        self.entries[slot] = DraftEntry(
            slot=slot,
            champion_id=champion_id,
            entered_at=entered_at or _now_iso(),
        )
        self.current_slot = slot + 1

    def amend(self, slot: int, champion_id: int, entered_at: str | None = None) -> list[int]:
        """Correction path for misclicks on an ALREADY-FILLED slot (slot < current_slot).

        Collisions are handled asymmetrically depending on direction, which is the only reading
        under which this method's two documented behaviors (hard-reject vs. flag-and-succeed)
        don't contradict each other:
          - A collision with an EARLIER filled slot (j < slot) is a hard error: that earlier
            champion was locked in first and is presumptively correct, so re-using its champion
            here is rejected outright.
          - A collision with a LATER filled slot (j > slot) is NOT rejected -- the amendment
            always succeeds, and the later slot's entry is instead flagged `invalidated=True`
            (never deleted -- auto-deleting could destroy a correct entry made several picks
            later) with its slot index returned so the caller (UI layer) can surface "this
            correction also invalidated your later pick at slot X, please fix that too."

        Re-assigning a slot to the champion it already holds is a no-op, not an error (trivial
        self-collision is explicitly allowed).
        """
        current = self.entries[slot] if 0 <= slot < len(self.entries) else None
        if current is None:
            raise DraftValidationError(f"slot {slot} is not filled; nothing to amend")

        if champion_id != current.champion_id and self._champion_used_in_earlier_slot(
            champion_id, before_slot=slot
        ):
            raise DraftValidationError(
                f"champion_id {champion_id} collides with an earlier currently-filled slot"
            )

        current.champion_id = champion_id
        current.entered_at = entered_at or _now_iso()
        current.amended_count += 1
        current.invalidated = False  # this slot itself is now a fresh, intentional entry

        newly_flagged: list[int] = []
        for j in range(slot + 1, len(self.entries)):
            other = self.entries[j]
            if other is not None and other.champion_id == champion_id and not other.invalidated:
                other.invalidated = True
                newly_flagged.append(j)
        return newly_flagged

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _champion_used_anywhere(self, champion_id: int, exclude_slot: int | None) -> bool:
        for entry in self.entries:
            if entry is None:
                continue
            if exclude_slot is not None and entry.slot == exclude_slot:
                continue
            if entry.champion_id == champion_id:
                return True
        return False

    def _champion_used_in_earlier_slot(self, champion_id: int, before_slot: int) -> bool:
        """True if `champion_id` occupies any filled slot strictly before `before_slot`. Used by
        amend() -- collisions with earlier slots are hard errors, collisions with later slots
        are not (see amend()'s docstring for why the two directions are handled asymmetrically)."""
        for entry in self.entries[:before_slot]:
            if entry is not None and entry.champion_id == champion_id:
                return True
        return False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "our_side": self.our_side,
            "current_slot": self.current_slot,
            "roster": [
                {"player_id": r.player_id, "role": r.role, "side": r.side} for r in self.roster
            ],
            "entries": [
                None
                if e is None
                else {
                    "slot": e.slot,
                    "champion_id": e.champion_id,
                    "entered_at": e.entered_at,
                    "amended_count": e.amended_count,
                    "invalidated": e.invalidated,
                }
                for e in self.entries
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DraftState":
        roster = [
            RosterAssignment(player_id=r["player_id"], role=r["role"], side=r["side"])
            for r in d["roster"]
        ]
        state = cls(our_side=d["our_side"], roster=roster)
        state.current_slot = d["current_slot"]
        state.entries = [
            None
            if e is None
            else DraftEntry(
                slot=e["slot"],
                champion_id=e["champion_id"],
                entered_at=e["entered_at"],
                amended_count=e.get("amended_count", 0),
                invalidated=e.get("invalidated", False),
            )
            for e in d["entries"]
        ]
        return state


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
