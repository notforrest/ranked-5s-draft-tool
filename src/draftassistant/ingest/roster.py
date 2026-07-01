"""Roster YAML ingestion.

The roster is a hand-editable file (data/curated/roster.yaml) the user maintains themselves --
one entry per friend, with their Riot ID and preferred role(s). This mirrors the same
hand-edited-file-plus-import-script pattern as the curated tier list (ingest/tier_list.py):
re-running the import after editing the file is how you add/update roster members, rather than
a CLI command per player.
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from draftassistant.db.repositories import roster_repo

logger = logging.getLogger(__name__)

PLATFORM_TO_ACCOUNT_REGION = {
    "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas", "oc1": "americas",
    "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe",
    "kr": "asia", "jp1": "asia",
}


def import_roster_file(conn, yaml_path: Path) -> dict:
    """Parses a roster YAML file and upserts every entry into players.

    Expected top-level shape:
        players:
          - display_name: "Forrest"
            riot_game_name: "ForrestSun"
            tag_line: "NA1"
            platform_region: "na1"       # optional, defaults to na1
            preferred_roles: ["MID"]     # optional, defaults to []

    Entries missing a required field, or naming an unrecognized platform_region, are skipped
    and counted (not crashed on) -- a typo in one friend's entry shouldn't block importing
    everyone else's.
    """
    yaml_path = Path(yaml_path)
    data = yaml.safe_load(yaml_path.read_text()) or {}
    entries = data.get("players") or []

    skipped: list[str] = []
    imported = 0
    for entry in entries:
        display_name = entry.get("display_name")
        riot_game_name = entry.get("riot_game_name")
        tag_line = entry.get("tag_line")
        label = display_name or riot_game_name or "(unnamed entry)"

        if not display_name or not riot_game_name or not tag_line:
            skipped.append(label)
            logger.warning("Roster entry %r missing display_name/riot_game_name/tag_line -- skipping.", label)
            continue

        platform_region = str(entry.get("platform_region", "na1")).lower()
        account_region = PLATFORM_TO_ACCOUNT_REGION.get(platform_region)
        if account_region is None:
            skipped.append(label)
            logger.warning("Roster entry %r has unknown platform_region %r (known: %s) -- skipping.",
                            label, platform_region, sorted(PLATFORM_TO_ACCOUNT_REGION))
            continue

        preferred_roles = [str(r).upper() for r in (entry.get("preferred_roles") or [])]

        roster_repo.upsert_player(
            conn,
            display_name=display_name,
            riot_game_name=riot_game_name,
            riot_tag_line=tag_line,
            platform_region=platform_region,
            account_region=account_region,
            preferred_roles=preferred_roles,
        )
        imported += 1

    return {
        "file": str(yaml_path),
        "entries_processed": len(entries),
        "entries_imported": imported,
        "entries_skipped": len(skipped),
        "skipped_entries": skipped,
    }
