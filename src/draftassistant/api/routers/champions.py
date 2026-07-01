"""Static champion data for the frontend's searchable champion grid."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from draftassistant.api.deps import get_db
from draftassistant.db.repositories import champion_repo

router = APIRouter(tags=["champions"])


@router.get("/champions")
def list_champions(conn: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    return champion_repo.get_all_champions(conn)
