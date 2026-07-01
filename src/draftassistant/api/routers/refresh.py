"""Pre-draft refresh trigger + status.

This router (and ONLY this router, besides `refresh/pre_draft_refresh.py` itself) is
allowed to import from `refresh/`. `draft.py` must never import from here, `refresh/`,
`riot_client/`, or `ingest/` -- see the module docstring in `draft.py` for the hard rule.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from draftassistant.api.deps import get_db
from draftassistant.db.repositories import match_repo
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
