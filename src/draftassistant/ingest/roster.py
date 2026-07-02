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

    Matching an entry to an existing player is normally by Riot ID (display_name alone isn't a
    stable identity -- someone could rename their display_name too). But Riot IDs themselves
    sometimes need correcting (a typo caught later, or a friend renamed in-game), and matching
    ONLY on Riot ID would treat that as a brand new player: the old row would stay behind,
    active, still holding its OLD (now-wrong) cached puuid, and still get pulled into every
    future refresh alongside the new one. So: if an entry's Riot ID doesn't match any existing
    player, but its display_name uniquely matches exactly one player that already existed
    BEFORE this import run under a DIFFERENT Riot ID, that's treated as a rename of that player
    rather than a new signup -- updating the existing row (and clearing its stale puuid) instead
    of inserting a duplicate.

    Rename-eligible players are taken from a snapshot of who existed before this run started,
    and each one is only usable as a rename target ONCE per run. Without this, two genuinely
    different NEW people who happen to share a display_name (e.g. two friends both named
    "Alex"), added in the same file, would incorrectly get merged into one row -- the second
    entry would see the first entry's just-inserted row as a "existing player with a different
    Riot ID" and rename it instead of creating its own. If display_name matches more than one
    still-available candidate, it's ambiguous -- skip the rename guess and fall through to a
    normal insert, with a warning (this can still under-merge in the rarer case of adding a
    brand new same-named person in the exact same run as a genuine rename; re-running the import
    afterward, or splitting them across two runs, resolves that).
    """
    yaml_path = Path(yaml_path)
    logger.info("Reading roster file %s...", yaml_path)
    data = yaml.safe_load(yaml_path.read_text()) or {}
    entries = data.get("players") or []
    logger.info("Found %d entr%s to process.", len(entries), "y" if len(entries) == 1 else "ies")

    existing_by_riot_id: dict[tuple[str, str], dict] = {}
    existing_by_name: dict[str, list[dict]] = {}
    for p in roster_repo.get_active_players(conn):
        existing_by_riot_id[(p["riot_game_name"], p["riot_tag_line"])] = p
        existing_by_name.setdefault(p["display_name"].lower(), []).append(p)

    skipped: list[str] = []
    renamed: list[str] = []
    imported = 0
    for i, entry in enumerate(entries):
        display_name = entry.get("display_name")
        logger.info("[%d/%d] processing %r...", i + 1, len(entries), display_name or entry.get("riot_game_name"))
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
        riot_key = (riot_game_name, tag_line)

        if riot_key not in existing_by_riot_id:
            candidates = [p for p in existing_by_name.get(display_name.lower(), [])
                          if (p["riot_game_name"], p["riot_tag_line"]) != riot_key]
            if len(candidates) == 1:
                old = candidates[0]
                roster_repo.update_riot_id(
                    conn, old["player_id"], riot_game_name=riot_game_name, riot_tag_line=tag_line,
                    platform_region=platform_region, account_region=account_region,
                    preferred_roles=preferred_roles,
                )
                renamed.append(
                    f"{display_name}: {old['riot_game_name']}#{old['riot_tag_line']} -> {riot_game_name}#{tag_line}"
                )
                logger.info("  -> detected Riot ID rename: %s#%s -> %s#%s",
                            old["riot_game_name"], old["riot_tag_line"], riot_game_name, tag_line)
                # Consumed -- later entries in this same file can't also match this identity.
                existing_by_name[display_name.lower()].remove(old)
                del existing_by_riot_id[(old["riot_game_name"], old["riot_tag_line"])]
                imported += 1
                continue
            if len(candidates) > 1:
                logger.warning(
                    "Roster entry %r has a new Riot ID but %d existing players share that "
                    "display_name -- can't tell which one this renames, adding as a new player "
                    "instead. Clean up manually (data/lol_draft.db `players` table) if needed.",
                    display_name, len(candidates),
                )

        roster_repo.upsert_player(
            conn,
            display_name=display_name,
            riot_game_name=riot_game_name,
            riot_tag_line=tag_line,
            platform_region=platform_region,
            account_region=account_region,
            preferred_roles=preferred_roles,
        )
        logger.info("  -> upserted.")
        imported += 1

    return {
        "file": str(yaml_path),
        "entries_processed": len(entries),
        "entries_imported": imported,
        "entries_skipped": len(skipped),
        "skipped_entries": skipped,
        "renamed": renamed,
    }
