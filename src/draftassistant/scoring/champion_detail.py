"""Champion-detail lookup for the hover/detail panel: real (non-normalized) global stats,
per-roster-player mastery/personal history, and pick-synergy / ban-threat detail for a single
champion against the CURRENT DraftState.

Deliberately separate from pick_score.py/ban_score.py: those modules produce normalized [0,1]
SCORING signals for ranking every candidate; this module produces human-readable real-world
numbers for direct display, computed fresh on-demand for exactly one champion rather than for
every available candidate. Reuses their role-inference helpers (already treated as
package-private-not-module-private in this codebase -- ban_score.py already imports several
underscored names from pick_score.py) rather than re-deriving role logic a third time.
"""
from __future__ import annotations

import sqlite3

from draftassistant import config
from draftassistant.db.repositories import (
    champion_repo,
    mastery_repo,
    personal_stats_repo,
    summoner_rank_repo,
    synergy_repo,
    tierlist_repo,
)
from draftassistant.draft import queries
from draftassistant.draft.state import DraftState
from draftassistant.scoring.ban_score import _their_unfilled_roles

_ROLE_ORDER = {role: i for i, role in enumerate(config.VALID_ROLES)}
from draftassistant.scoring.pick_score import _best_tier_role, _relevant_role_for_candidate


def _resolve_role(
    conn: sqlite3.Connection, champion_id: int, state: DraftState,
    action: str, requested_role: str | None,
) -> tuple[str | None, str]:
    """Returns (role, role_source). `role_source` is "requested" only if requested_role was
    both supplied AND the champion has some eligibility row for it -- a caller-supplied role the
    champion never plays falls back to inference rather than being trusted blindly."""
    eligibility = champion_repo.get_role_eligibility(conn, champion_id)
    if requested_role is not None and requested_role in eligibility:
        return requested_role, "requested"

    if action == "PICK":
        unfilled = queries.unfilled_roles(state, conn)
        return _relevant_role_for_candidate(conn, champion_id, unfilled), "inferred"
    # BAN: no single "our" role applies -- same best-global-role fallback ban_score.py uses.
    patch = tierlist_repo.get_latest_patch(conn) or ""
    return _best_tier_role(conn, champion_id, patch), "inferred"


def _global_stats(conn: sqlite3.Connection, champion_id: int, role: str | None, patch: str | None) -> dict:
    """ban_rate was removed then re-added (both 2026-07-01): OP.GG's REST auto-fetch
    (ingest/tier_list.py:import_from_opgg) has no ban rate in its response at all, so it was
    dropped from here rather than shown-but-usually-empty. It's back now that
    ingest/opgg_mcp_ingest.py's import_lane_meta (OP.GG's official MCP server, a separate source)
    populates it for real -- get_tier_entry doesn't care which source_note wrote the row it
    finds, so this needs no source-specific branching, just whichever row is latest for this
    (champion, role, patch)."""
    if role is None or patch is None:
        return {"win_rate": None, "pick_rate": None, "ban_rate": None, "tier": None,
                "sample_size": None, "has_data": False}
    entry = tierlist_repo.get_tier_entry(conn, champion_id, role, patch)
    if entry is None:
        return {"win_rate": None, "pick_rate": None, "ban_rate": None, "tier": None,
                "sample_size": None, "has_data": False}
    return {
        "win_rate": entry["win_rate"], "pick_rate": entry["pick_rate"],
        "ban_rate": entry["ban_rate"], "tier": entry["tier"],
        "sample_size": entry["sample_size"], "has_data": True,
    }


def _roster_rows(conn: sqlite3.Connection, state: DraftState, roster_lookup: dict[int, dict],
                  champion_id: int, role: str | None) -> list[dict]:
    """Rows are ordered TOP/JUNGLE/MID/BOTTOM/SUPPORT (config.VALID_ROLES), not state.roster's
    raw storage order -- roster assignment order isn't guaranteed to be lane order, and a driver
    scanning the hover panel mid-draft expects a stable, familiar lane ordering every time."""
    rows = []
    for assignment in sorted(state.roster, key=lambda a: _ROLE_ORDER.get(a.role, len(_ROLE_ORDER))):
        player_id = assignment.player_id
        meta = roster_lookup.get(player_id, {})
        mastery_row = mastery_repo.get_mastery(conn, player_id, champion_id)
        personal_row = personal_stats_repo.get_personal_stats(conn, player_id, champion_id)
        rank_row = summoner_rank_repo.get_player_rank(conn, player_id, "SOLORANKED")
        rows.append({
            "player_id": player_id,
            "display_name": meta.get("display_name", f"Player {player_id}"),
            "assigned_role": assignment.role,
            "is_assigned_to_role": role is not None and assignment.role == role,
            "mastery": None if mastery_row is None else {
                "level": mastery_row["champion_level"], "points": mastery_row["champion_points"],
            },
            "personal": None if personal_row is None or not personal_row["games"] else {
                "games": personal_row["games"], "wins": personal_row["wins"],
                "win_rate": personal_row["wins"] / personal_row["games"],
            },
            # From ingest/opgg_mcp_ingest.py's import_summoner_ranks -- null until that's been
            # run at least once. division is present even for apex tiers (confirmed live) --
            # frontend formatting (app.js's formatRank) checks the tier name, not division, to
            # decide whether to display one.
            "rank": None if rank_row is None or rank_row["tier"] is None else {
                "tier": rank_row["tier"], "division": rank_row["division"], "lp": rank_row["lp"],
            },
        })
    return rows


def _synergy_with_picks(conn: sqlite3.Connection, champ_by_id: dict[int, dict],
                         allies: list[int], champion_id: int) -> list[dict]:
    out = []
    for ally_id in allies:
        # Preference order: our own roster's games, then OP.GG's much-larger aggregate
        # population (ingest/opgg_mcp_ingest.py), then curated pro games last -- roster data is
        # most personally relevant when it exists; opgg's population size beats pro's narrower,
        # slower-to-update sample otherwise.
        for table, source in (("synergy_roster", "roster"), ("synergy_opgg", "opgg"), ("synergy_pro", "pro")):
            row = synergy_repo.get_synergy(conn, table, ally_id, champion_id)
            if row is None or row["games_together"] <= 0:
                continue
            out.append({
                "ally_champion_id": ally_id,
                "ally_champion_name": champ_by_id.get(ally_id, {}).get("name", f"#{ally_id}"),
                "games_together": row["games_together"],
                "win_rate_together": row["wins_together"] / row["games_together"],
                "source": source,
            })
            break  # prefer roster data over pro for the same pair when both exist
    return out


def _ban_threat(conn: sqlite3.Connection, state: DraftState, roster_lookup: dict[int, dict],
                 champ_by_id: dict[int, dict], champion_id: int, role: str | None) -> dict:
    counters = []
    for assignment in state.roster:
        distribution = mastery_repo.get_player_mastery_distribution(conn, assignment.player_id)
        top_3 = [d["champion_id"] for d in distribution[:3]]
        for comfort_id in top_3:
            for table, source in (("matchup_roster", "roster"), ("matchup_opgg", "opgg"), ("matchup_pro", "pro")):
                row = synergy_repo.get_matchup(conn, table, champion_id, comfort_id)
                if row is None or row["games"] <= 0:
                    continue
                counters.append({
                    "player_id": assignment.player_id,
                    "display_name": roster_lookup.get(assignment.player_id, {}).get(
                        "display_name", f"Player {assignment.player_id}"),
                    "comfort_champion_id": comfort_id,
                    "comfort_champion_name": champ_by_id.get(comfort_id, {}).get("name", f"#{comfort_id}"),
                    "games": row["games"],
                    "win_rate_against": row["wins_a"] / row["games"],
                    "source": source,
                })
                break

    enemy_fits_role = None
    their_picks = queries.their_picks_so_far(state)
    if their_picks and role is not None:
        their_unfilled = _their_unfilled_roles(state, conn)
        if their_unfilled and role == their_unfilled[0]:
            enemy_fits_role = role

    return {"counters_comfort_pool": counters, "enemy_fits_role": enemy_fits_role}


def get_champion_detail(
    conn: sqlite3.Connection, state: DraftState, champion_id: int,
    roster_lookup: dict[int, dict], requested_role: str | None = None,
) -> dict:
    """`roster_lookup`: {player_id: {"display_name": str}} -- caller-supplied since DraftState's
    own roster only carries player_id/role/side, not display_name (see draft/state.py)."""
    slot_info = queries.current_slot_info(state)
    action = slot_info["action"] or "PICK"  # draft-complete edge case: this endpoint isn't
                                              # normally reachable there (the frontend hides
                                              # suggestions once the draft is done), so this is
                                              # a defensive default, not a real user-facing path.
    patch = tierlist_repo.get_latest_patch(conn)

    role, role_source = _resolve_role(conn, champion_id, state, action, requested_role)
    champ_by_id = {c["champion_id"]: c for c in champion_repo.get_all_champions(conn)}

    result = {
        "champion_id": champion_id,
        "champion_name": champ_by_id.get(champion_id, {}).get("name", f"#{champion_id}"),
        "role": role,
        "role_source": role_source,
        "patch": patch,
        "action_context": action,
        "global": _global_stats(conn, champion_id, role, patch),
        "roster": _roster_rows(conn, state, roster_lookup, champion_id, role),
        "synergy_with_picks": [],
        "ban_threat": None,
    }
    if action == "PICK":
        allies = queries.our_picks_so_far(state)
        result["synergy_with_picks"] = _synergy_with_picks(conn, champ_by_id, allies, champion_id)
    else:
        result["ban_threat"] = _ban_threat(conn, state, roster_lookup, champ_by_id, champion_id, role)

    return result
