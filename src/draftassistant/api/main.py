"""FastAPI app factory. Mounts all API routers under /api, then serves the static
frontend from api/static/ at "/" so opening http://localhost:8000/ loads the app
directly -- same-origin, so no CORS configuration is needed."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from draftassistant.api.routers import champions, draft, refresh, roster

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="Ranked 5s Draft Tool")

    app.include_router(roster.router, prefix="/api")
    app.include_router(refresh.router, prefix="/api")
    app.include_router(champions.router, prefix="/api")
    app.include_router(draft.router, prefix="/api")

    # Mounted last: StaticFiles(html=True) serves index.html for "/" and would otherwise
    # shadow any router path registered after it.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app


app = create_app()
