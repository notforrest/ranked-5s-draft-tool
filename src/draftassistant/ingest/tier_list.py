"""Curated global tier-list YAML ingestion.

The tier list is a hand-editable file (data/curated/tier_list/<patch>.yaml) the user maintains
themselves by copying numbers off a third-party site (u.gg/op.gg/lolalytics) -- Riot's API does
not expose global win/pick/ban rate data, so this is deliberately a manual, low-frequency import,
not something fetched automatically.
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from draftassistant.db.repositories import champion_repo, tierlist_repo

logger = logging.getLogger(__name__)


def import_tier_list_file(conn, yaml_path: Path) -> dict:
    """Parses a tier-list YAML file and upserts every entry into global_tier_list.

    Expected top-level shape:
        patch: "14.13"
        source_note: "..."
        entries:
          - champion: "Ahri"
            role: "MID"
            win_rate: 0.51
            pick_rate: 0.08
            ban_rate: 0.02
            tier: "A"
            sample_size: 12000

    Entries whose `champion` name fails to resolve against the champions table are skipped and
    counted (not crashed on), since a typo'd or not-yet-DDragon-synced champion name shouldn't
    abort the whole file's import.
    """
    yaml_path = Path(yaml_path)
    data = yaml.safe_load(yaml_path.read_text())

    patch = data.get("patch")
    source_note = data.get("source_note")
    entries = data.get("entries") or []

    unresolved: list[str] = []
    imported = 0
    for entry in entries:
        champion_name = entry.get("champion")
        champ = champion_repo.get_champion_by_name(conn, champion_name)
        if champ is None:
            unresolved.append(str(champion_name))
            logger.warning("Unresolved tier-list champion name %r in %s -- check spelling or "
                            "run the DDragon champion sync first.", champion_name, yaml_path)
            continue

        tierlist_repo.upsert_tier_entry(
            conn,
            champion_id=champ["champion_id"],
            role=entry.get("role"),
            patch=patch,
            win_rate=entry.get("win_rate"),
            pick_rate=entry.get("pick_rate"),
            ban_rate=entry.get("ban_rate"),
            tier=entry.get("tier"),
            sample_size=entry.get("sample_size"),
            source_note=source_note,
        )
        imported += 1

    return {
        "file": str(yaml_path),
        "patch": patch,
        "entries_processed": len(entries),
        "entries_imported": imported,
        "entries_unresolved": len(unresolved),
        "unresolved_champion_names": sorted(set(unresolved)),
    }
