"""Roster CRUD + session/lineup creation.

Only touches `roster_repo` -- no Riot API / refresh / ingest imports here (this router
isn't covered by the hard "draft.py must not import riot_client/refresh/ingest" rule,
but there's no reason for it to reach into those layers either)."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from draftassistant import config
from draftassistant.api.deps import get_db
from draftassistant.db.repositories import roster_repo, summoner_rank_repo

router = APIRouter(tags=["roster"])


# ---------------------------------------------------------------------------
# GET /api/roster
# ---------------------------------------------------------------------------
@router.get("/roster")
def list_roster(conn: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    """Enriches each player with their SOLORANKED standing (null until
    ingest/opgg_mcp_ingest.py's import_summoner_ranks has been run at least once) so the setup
    screen's role-assignment list can show it -- avoids a second round trip for what's otherwise
    a small, cheap per-player lookup."""
    players = roster_repo.get_active_players(conn)
    for player in players:
        rank_row = summoner_rank_repo.get_player_rank(conn, player["player_id"], "SOLORANKED")
        player["rank"] = (
            None if rank_row is None or rank_row["tier"] is None
            else {"tier": rank_row["tier"], "division": rank_row["division"], "lp": rank_row["lp"]}
        )
    return players


# ---------------------------------------------------------------------------
# POST /api/roster
# ---------------------------------------------------------------------------
class AddPlayerRequest(BaseModel):
    display_name: str
    riot_game_name: str
    riot_tag_line: str
    platform_region: str
    account_region: str
    preferred_roles: list[str] | None = None


@router.post("/roster", status_code=201)
def add_player(body: AddPlayerRequest, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    if body.account_region not in config.VALID_ACCOUNT_REGIONS:
        # account-v1 (Riot ID -> PUUID resolution) uses *regional* routing (americas/asia/europe),
        # not platform routing (na1/euw1/...) -- a very common mixup, so reject clearly here
        # rather than letting a bad region silently fail later inside the refresh job.
        raise HTTPException(
            status_code=400,
            detail=(
                f"account_region must be one of {config.VALID_ACCOUNT_REGIONS} "
                f"(got {body.account_region!r}). Note: this is Riot's *regional* routing "
                "value used by Account-V1, not a platform routing value like 'na1'."
            ),
        )

    player_id = roster_repo.upsert_player(
        conn,
        display_name=body.display_name,
        riot_game_name=body.riot_game_name,
        riot_tag_line=body.riot_tag_line,
        platform_region=body.platform_region,
        account_region=body.account_region,
        preferred_roles=body.preferred_roles,
    )
    return {"player_id": player_id}


# ---------------------------------------------------------------------------
# POST /api/roster/sessions
# ---------------------------------------------------------------------------
class LineupEntry(BaseModel):
    player_id: int
    assigned_role: str


class CreateSessionRequest(BaseModel):
    our_side: str
    lineup: list[LineupEntry] = Field(min_length=5, max_length=5)


@router.post("/roster/sessions", status_code=201)
def create_session(body: CreateSessionRequest, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    if body.our_side not in ("BLUE", "RED"):
        raise HTTPException(status_code=400, detail="our_side must be 'BLUE' or 'RED'")

    if len(body.lineup) != 5:
        raise HTTPException(status_code=400, detail="lineup must have exactly 5 entries")

    assigned_roles = [entry.assigned_role for entry in body.lineup]
    if sorted(assigned_roles) != sorted(config.VALID_ROLES):
        raise HTTPException(
            status_code=400,
            detail=(
                "lineup must cover all 5 standard roles "
                f"({', '.join(config.VALID_ROLES)}) exactly once each "
                f"(got {assigned_roles})"
            ),
        )

    player_ids = [entry.player_id for entry in body.lineup]
    if len(set(player_ids)) != 5:
        raise HTTPException(status_code=400, detail="lineup must name 5 distinct players")

    session_id = roster_repo.create_session(
        conn,
        our_side=body.our_side,
        lineup=[(entry.player_id, entry.assigned_role) for entry in body.lineup],
    )
    return {"session_id": session_id}
