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

from draftassistant import config
from draftassistant.draft.sequence import DRAFT_SEQUENCE, PICK


class DraftValidationError(Exception):
    """Raised when a caller attempts an illegal enter()/undo_last() against the current state."""


@dataclass
class DraftEntry:
    slot: int
    champion_id: int
    entered_at: str  # ISO-8601 timestamp string, caller/UI-supplied precision
    role_override: str | None = None  # user-set role, takes precedence over auto-resolution
                                        # (see draft/queries.py:resolve_pick_roles); None means
                                        # "no override, use the auto-resolved role"


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

    def undo_last(self) -> None:
        """Reverts the single most recently entered slot (current_slot - 1), clearing it back
        to empty and moving current_slot back onto it. Only ever targets the LATEST filled slot
        -- there's no way to reach back and fix an arbitrary earlier one, and no collision
        handling is needed since removing the most-advanced entry can't create a duplicate
        anywhere else on the board. Raises DraftValidationError (state untouched) if there is
        nothing to undo (current_slot == 0)."""
        if self.current_slot == 0:
            raise DraftValidationError("nothing to undo -- no picks or bans have been entered yet")

        last_slot = self.current_slot - 1
        self.entries[last_slot] = None
        self.current_slot = last_slot

    def set_role_override(self, slot: int, role: str) -> None:
        """Manually pins `slot`'s role, overriding whatever draft/queries.py's
        resolve_pick_roles would otherwise auto-compute for it. Valid only against an
        already-filled PICK slot for OUR side -- there is no role concept for a ban, an
        opponent's pick (we don't drive their roster), or a slot that hasn't been entered yet.
        Raises DraftValidationError and leaves state untouched on any violation, consistent with
        enter()'s existing convention."""
        entry = self._require_filled(slot)
        slot_def = DRAFT_SEQUENCE[slot]
        if slot_def.action != PICK:
            raise DraftValidationError(f"slot {slot} is a {slot_def.action}, not a PICK; role overrides only apply to picks")
        if slot_def.side != self.our_side:
            raise DraftValidationError(f"slot {slot} is the opponent's pick; we don't assign roles to their roster")
        if role not in config.VALID_ROLES:
            raise DraftValidationError(f"role {role!r} is not a valid role; must be one of {config.VALID_ROLES}")
        entry.role_override = role

    def clear_role_override(self, slot: int) -> None:
        """Removes a manual override, reverting `slot` to auto-resolution. A no-op (not an
        error) if the slot had no override set -- clearing an already-clear thing is harmless,
        not a caller mistake."""
        entry = self._require_filled(slot)
        entry.role_override = None

    def _require_filled(self, slot: int) -> DraftEntry:
        if not 0 <= slot < len(self.entries):
            raise DraftValidationError(f"slot {slot} is out of range [0, {len(self.entries)})")
        entry = self.entries[slot]
        if entry is None:
            raise DraftValidationError(f"slot {slot} is not filled; nothing to set a role override on")
        return entry

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
                    "role_override": e.role_override,
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
                role_override=e.get("role_override"),
            )
            for e in d["entries"]
        ]
        return state


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
