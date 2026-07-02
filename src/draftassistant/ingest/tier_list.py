"""Global tier-list ingestion -- two paths into the same `global_tier_list` table.

Riot's own API does not expose global win/pick/ban rate data at all, so this data has to come
from a third-party tier-list site one way or another:

1. `import_tier_list_file` -- a hand-editable YAML file (data/curated/tier_list/<patch>.yaml)
   the user maintains themselves by copying numbers off a site of their choice. Always works,
   any source, but manual.
2. `import_from_opgg` -- automated, via staticdata/opgg_client.py's unofficial OP.GG API call.
   No manual copying needed, but it's a best-effort scrape of an undocumented endpoint that can
   break if OP.GG changes it (see opgg_client.py's docstring for the reasoning behind choosing
   OP.GG specifically). Rows from either path share the same table and schema; source_note
   records which one produced a given row.
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from draftassistant.db.repositories import champion_repo, tierlist_repo
from draftassistant.staticdata import opgg_client

logger = logging.getLogger(__name__)

_OPGG_POSITION_TO_ROLE = {
    "TOP": "TOP", "JUNGLE": "JUNGLE", "MID": "MID", "SUPPORT": "SUPPORT", "ADC": "BOTTOM",
}


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
    logger.info("Reading tier-list file %s...", yaml_path)
    data = yaml.safe_load(yaml_path.read_text())

    patch = data.get("patch")
    source_note = data.get("source_note")
    entries = data.get("entries") or []
    logger.info("Patch %s: importing %d entr%s.", patch, len(entries), "y" if len(entries) == 1 else "ies")

    unresolved: list[str] = []
    imported = 0
    for i, entry in enumerate(entries):
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
        if (i + 1) % 25 == 0 or (i + 1) == len(entries):
            logger.info("  ...%d/%d entries processed", i + 1, len(entries))

    return {
        "file": str(yaml_path),
        "patch": patch,
        "entries_processed": len(entries),
        "entries_imported": imported,
        "entries_unresolved": len(unresolved),
        "unresolved_champion_names": sorted(set(unresolved)),
    }


def _normalize_patch(version: str | None) -> str | None:
    """"14.24.1" -> "14.24" (major.minor), matching the convention used elsewhere (see
    refresh/pre_draft_refresh.py's _derive_patch). Falls back to the raw string unchanged if it
    doesn't look like a dotted version (OP.GG's own versioning scheme isn't documented)."""
    if not version:
        return version
    parts = version.split(".")
    if len(parts) < 2:
        return version
    return f"{parts[0]}.{parts[1]}"


def import_from_opgg(conn, mode: str = "ranked") -> dict:
    """Fetches global champion win/pick rate from OP.GG's unofficial API (one HTTP request) and
    upserts it into global_tier_list -- the automated counterpart to import_tier_list_file.

    For each champion, prefer per-position stats when OP.GG's response includes them (more
    precise win_rate); when it doesn't, fall back to the champion's overall average_stats
    applied to every role this app already knows the champion is played in (via
    champion_role_eligibility, populated from personal/pro match data) -- a champion with no
    known eligible role yet is skipped for this run rather than guessed at.

    pick_rate is always the champion's OVERALL pick rate (average_stats), not position-specific
    -- OP.GG's per-position figure represents "% of this champion's own games in this position,"
    which isn't the same statistic as "market share among all picks in this role" and would be
    misleading to present as if it were. This is a known simplification, documented here rather
    than silently implied. `tier`/`ban_rate` aren't populated by this path (OP.GG's numeric tier
    ranking isn't a letter grade and isn't worth guessing a translation for; ban_rate isn't in
    this endpoint's response at all) -- both remain available via the manual YAML path if wanted.

    Also populates champion_role_eligibility (source "opgg") from positions[].stats.play when
    positions data is present -- without this, a champion picked in a live draft with NO other
    role-data source (no Oracle's Elixir import, no personal Riot refresh run yet) would never
    have its role recognized as "filled", and role-gated suggestions would keep offering that
    role indefinitely. Uses `play` (games in that position), not `win`, since games_observed is
    a tie-break magnitude elsewhere (champion_repo.get_role_eligibility/_best_matching_role),
    never a rate -- matching how `sample_size=stats.get("play")` is already used two lines below
    for the tier-list row itself. The no-positions fallback branch is NOT extended to write a
    synthetic eligibility row: it only ever runs for a champion that already has an eligibility
    row from another source (it skips entirely when none exists), so there's no independently
    role-scoped signal in that branch to derive one from -- the manual role-override feature
    (draft/state.py's set_role_override) is the intended fallback for those champions instead.
    """
    logger.info("Fetching %s champion stats from OP.GG...", mode)
    payload = opgg_client.fetch_champion_stats(mode=mode)
    patch = _normalize_patch(payload.get("meta", {}).get("version"))
    source_note = f"Auto-fetched from op.gg (unofficial API), meta.version={payload.get('meta', {}).get('version')}"
    champions_in_payload = payload.get("data") or []
    logger.info("Fetched patch %s, %d champion(s) in response. Importing...", patch, len(champions_in_payload))

    imported = 0
    skipped_unresolved_champion: list[int] = []
    skipped_no_role: list[int] = []
    opgg_eligibility_rows: list[tuple[int, str, int]] = []

    for i, summary in enumerate(champions_in_payload):
        champion_id = summary.get("id")
        if champion_repo.get_champion_by_id(conn, champion_id) is None:
            skipped_unresolved_champion.append(champion_id)
            continue

        average_stats = summary.get("average_stats") or {}
        overall_pick_rate = average_stats.get("pick_rate")
        positions = summary.get("positions") or []

        if positions:
            for position in positions:
                role = _OPGG_POSITION_TO_ROLE.get(position.get("name"))
                if role is None:
                    continue
                stats = position.get("stats") or {}
                tierlist_repo.upsert_tier_entry(
                    conn, champion_id=champion_id, role=role, patch=patch,
                    win_rate=stats.get("win_rate"), pick_rate=overall_pick_rate,
                    ban_rate=None, tier=None, sample_size=stats.get("play"),
                    source_note=source_note,
                )
                imported += 1
                play = stats.get("play")
                if play:
                    opgg_eligibility_rows.append((champion_id, role, play))
        else:
            eligible_roles = champion_repo.get_role_eligibility(conn, champion_id)
            if not eligible_roles:
                skipped_no_role.append(champion_id)
                continue
            for role in eligible_roles:
                tierlist_repo.upsert_tier_entry(
                    conn, champion_id=champion_id, role=role, patch=patch,
                    win_rate=average_stats.get("win_rate"), pick_rate=overall_pick_rate,
                    ban_rate=None, tier=None, sample_size=None,
                    source_note=source_note,
                )
                imported += 1

        if (i + 1) % 25 == 0 or (i + 1) == len(champions_in_payload):
            logger.info("  ...%d/%d champions processed (%d tier-list entries imported so far)",
                        i + 1, len(champions_in_payload), imported)

    # Always called, even with an empty list -- a run that (this time) sees zero
    # positions-bearing champions correctly wipes any stale "opgg" rows from a previous run,
    # consistent with replace_role_eligibility's documented "wholesale replace per source" contract.
    logger.info("Recomputing opgg-sourced role eligibility (%d row(s))...", len(opgg_eligibility_rows))
    champion_repo.replace_role_eligibility(conn, "opgg", opgg_eligibility_rows)

    return {
        "source": "opgg",
        "mode": mode,
        "patch": patch,
        "champions_processed": len(champions_in_payload),
        "entries_imported": imported,
        "champions_skipped_unresolved": skipped_unresolved_champion,
        "champions_skipped_no_known_role": skipped_no_role,
    }
