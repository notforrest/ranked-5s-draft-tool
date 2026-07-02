"""Pre-draft refresh trigger + status, plus meta-data fetching (global tier list, Oracle's
Elixir).

This router (and ONLY this router, besides the underlying `refresh/`/`ingest/` modules
themselves) is allowed to import from `refresh/` and `ingest/`. `draft.py` must never import
from here, `refresh/`, `riot_client/`, `ingest/`, or `staticdata/` -- see the module docstring
in `draft.py` for the hard rule. Every endpoint here is an external-data-gathering operation
meant to run before a draft, never during one.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from draftassistant import config
from draftassistant.api.deps import get_db
from draftassistant.db.repositories import match_repo
from draftassistant.ingest import oracles_elixir, opgg_mcp_ingest, tier_list
from draftassistant.refresh import pre_draft_refresh

router = APIRouter(tags=["refresh"])


# ---------------------------------------------------------------------------
# POST /api/refresh
# ---------------------------------------------------------------------------
@router.post("/refresh")
def run_refresh(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """Synchronously runs the pre-draft refresh job. This makes real Riot API network
    calls and may take a while -- that's expected, since it's meant to run before
    queuing up, never during a live draft (see the architectural rule in draft.py)."""
    return pre_draft_refresh.run(conn)


# ---------------------------------------------------------------------------
# GET /api/refresh/latest
# ---------------------------------------------------------------------------
@router.get("/refresh/latest")
def latest_refresh(conn: sqlite3.Connection = Depends(get_db)) -> dict | None:
    """So the setup screen can show 'last refreshed: ...' without re-running anything."""
    return match_repo.get_latest_refresh_run(conn)


# ---------------------------------------------------------------------------
# POST /api/refresh/tier-list
# ---------------------------------------------------------------------------
class TierListFetchRequest(BaseModel):
    mode: str = "ranked"


@router.post("/refresh/tier-list")
def fetch_tier_list(
    body: TierListFetchRequest = TierListFetchRequest(), conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    """Automated global win/pick rate fetch from OP.GG -- unofficial and best-effort (see
    ingest/tier_list.py and staticdata/opgg_client.py docstrings for why OP.GG specifically, and
    why this can't be a guaranteed-stable data source). Wrapped so a broken/changed upstream
    endpoint surfaces as a clear message, not a raw traceback -- the hand-maintained
    data/curated/tier_list/*.yaml + import_tier_list.py path is unaffected either way."""
    try:
        return tier_list.import_from_opgg(conn, mode=body.mode)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Tier list fetch from op.gg failed: {e}") from e


# ---------------------------------------------------------------------------
# GET /api/refresh/oracles-elixir/status
# ---------------------------------------------------------------------------
@router.get("/refresh/oracles-elixir/status")
def oracles_elixir_status() -> dict:
    """So the setup screen knows whether to offer 'fetch from configured URL' alongside
    'upload a file', or just the upload option."""
    return {"configured_url_set": bool(config.ORACLES_ELIXIR_CSV_URL)}


# ---------------------------------------------------------------------------
# POST /api/refresh/oracles-elixir/fetch
# ---------------------------------------------------------------------------
@router.post("/refresh/oracles-elixir/fetch")
def fetch_oracles_elixir(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """Downloads from the ORACLES_ELIXIR_CSV_URL configured in .env and imports it. The exact
    current oracleselixir.com link isn't stable/hardcodable long-term, so this only works once
    that setting has been filled in by hand -- see .env.example."""
    if not config.ORACLES_ELIXIR_CSV_URL:
        raise HTTPException(
            status_code=400,
            detail="No ORACLES_ELIXIR_CSV_URL configured in .env -- use the Upload option instead, "
                   "or grab the current link from oracleselixir.com/tools/downloads and set it.",
        )
    try:
        return oracles_elixir.fetch_from_url(conn, config.ORACLES_ELIXIR_CSV_URL)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Oracle's Elixir fetch failed: {e}") from e


# ---------------------------------------------------------------------------
# POST /api/refresh/oracles-elixir/upload
# ---------------------------------------------------------------------------
@router.post("/refresh/oracles-elixir/upload")
async def upload_oracles_elixir(
    file: UploadFile = File(...), conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    """Accepts a manually-downloaded Oracle's Elixir CSV from the setup screen -- always works
    regardless of network access or link staleness, the fallback for when there's no
    ORACLES_ELIXIR_CSV_URL configured (or it's gone stale)."""
    content = await file.read()
    try:
        return oracles_elixir.import_csv_bytes(conn, file.filename or "uploaded.csv", content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not import file: {e}") from e


# ---------------------------------------------------------------------------
# POST /api/refresh/opgg-mcp/lane-meta
# ---------------------------------------------------------------------------
@router.post("/refresh/opgg-mcp/lane-meta")
async def fetch_opgg_mcp_lane_meta(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """Fetches win/pick/**ban** rate for all 5 lanes from OP.GG's official MCP server
    (mcp-api.op.gg) -- unlike fetch_tier_list's REST scrape above, this source actually includes
    ban rate. Prototype: live-verified 2026-07-01 but not load-tested -- see
    staticdata/opgg_mcp_client.py's module docstring for exactly what's confirmed."""
    try:
        return await opgg_mcp_ingest.import_lane_meta(conn)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OP.GG MCP lane-meta fetch failed: {e}") from e


# ---------------------------------------------------------------------------
# POST /api/refresh/opgg-mcp/summoner-ranks
# ---------------------------------------------------------------------------
@router.post("/refresh/opgg-mcp/summoner-ranks")
async def fetch_opgg_mcp_summoner_ranks(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """Fetches rank/tier/LP for every active roster player -- no other data source in this
    codebase has rank/LP at all. Per-player failures are isolated in the response, not fatal."""
    try:
        return await opgg_mcp_ingest.import_summoner_ranks(conn)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OP.GG MCP summoner-rank fetch failed: {e}") from e


# ---------------------------------------------------------------------------
# POST /api/refresh/opgg-mcp/champion-analysis
# ---------------------------------------------------------------------------
class ChampionAnalysisFetchRequest(BaseModel):
    top_n_per_lane: int = 10


@router.post("/refresh/opgg-mcp/champion-analysis")
async def fetch_opgg_mcp_champion_analysis(
    body: ChampionAnalysisFetchRequest = ChampionAnalysisFetchRequest(),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Counter-matchup and ally-synergy data for the top N champions per lane (by the lane-meta
    fetch above's sample size) -- NOT all ~170 champions x 5 positions, which would be ~850
    individual calls at ~2s each. Run fetch_opgg_mcp_lane_meta first; this reads from whatever
    it most recently stored. This is the slowest of the three OP.GG MCP fetches (roughly
    2 seconds x top_n_per_lane x 5 lanes) -- the frontend should warn accordingly."""
    try:
        champion_positions = await opgg_mcp_ingest.default_champion_positions_from_lane_meta(
            conn, top_n_per_lane=body.top_n_per_lane,
        )
        if not champion_positions:
            raise HTTPException(
                status_code=400,
                detail="No opgg-mcp lane-meta data found -- run 'Fetch Ban Rate + Tier List' first.",
            )
        return await opgg_mcp_ingest.import_champion_matchups_and_synergies(conn, champion_positions)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OP.GG MCP champion-analysis fetch failed: {e}") from e
