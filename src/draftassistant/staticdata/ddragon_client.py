"""Data Dragon (DDragon) static-asset client -- champion names/icons/tags.

DDragon is Riot's public, unauthenticated static-asset CDN (https://ddragon.leagueoflegends.com).
No API key needed, no rate limiting concerns (it's a CDN, not the rate-limited game API), no
RiotAPIClient involved -- just plain httpx calls.
"""
from __future__ import annotations

import json

import httpx

from draftassistant import config
from draftassistant.db.repositories import champion_repo

_BASE = "https://ddragon.leagueoflegends.com"
_TIMEOUT_S = 10.0


def get_latest_version() -> str:
    """Returns the current live patch's DDragon version string, e.g. "14.13.1"."""
    resp = httpx.get(f"{_BASE}/api/versions.json", timeout=_TIMEOUT_S)
    resp.raise_for_status()
    versions = resp.json()
    return versions[0]


def sync_champions(conn) -> dict:
    """Fetches the full champion list for the latest DDragon version and upserts every champion
    into the `champions` table. Stores the FULL absolute CDN URL in icon_path so the frontend can
    use it directly as an <img src> with no further construction needed.

    Caches the raw champion.json response under config.DDRAGON_CACHE_DIR keyed by version, so
    re-running this doesn't always redownload (champion data for an already-fetched version never
    changes).

    Does NOT commit the connection -- the caller commits (or lets sqlite3's autocommit/context
    manager do so), keeping this function's side effects limited to the connection it was given.
    """
    version = get_latest_version()
    data = _get_champion_json(version)

    champions_data = data["data"]
    for entry in champions_data.values():
        champion_repo.upsert_champion(
            conn,
            champion_id=int(entry["key"]),
            champion_key=entry["id"],
            name=entry["name"],
            tags=entry["tags"],
            icon_path=f"{_BASE}/cdn/{version}/img/champion/{entry['image']['full']}",
            ddragon_version=version,
        )

    return {"version": version, "champions_synced": len(champions_data)}


def _get_champion_json(version: str) -> dict:
    cache_path = config.DDRAGON_CACHE_DIR / f"champion_{version}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text())

    resp = httpx.get(f"{_BASE}/cdn/{version}/data/en_US/champion.json", timeout=_TIMEOUT_S)
    resp.raise_for_status()
    data = resp.json()

    config.DDRAGON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(data))
    return data
