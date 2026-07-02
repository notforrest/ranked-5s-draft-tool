"""OP.GG MCP-sourced data ingestion -- the async counterpart to ingest/tier_list.py's OP.GG REST
scrape and refresh/pre_draft_refresh.py's Riot API refresh. Kept in its own module (not merged
into either) since it's a different transport (MCP, async) and closes genuinely different gaps:
ban rate (the REST scrape has none at all), rank/LP (nothing else in this codebase has any), and
a third, larger-population matchup/synergy source alongside synergy_roster/synergy_pro.

Like pre_draft_refresh.py, this must NEVER be called from the live draft screen's request path --
same architectural rule, same reasoning (a network hiccup here must never break an in-progress
draft; see draft.py's module docstring for the hard rule this mirrors). Unlike
pre_draft_refresh.py these functions are async (the MCP client is asyncio-native); callers bridge
with FastAPI's native async route support or asyncio.run() in a script, not inside this module.
"""
from __future__ import annotations

import logging
import re

from draftassistant.db.repositories import champion_repo, roster_repo, summoner_rank_repo, synergy_repo, tierlist_repo
from draftassistant.staticdata import opgg_mcp_client

logger = logging.getLogger(__name__)

# Best-effort mapping from Riot's platform region id (players.platform_region, e.g. "na1") to
# OP.GG's own short region code (e.g. "NA") that the MCP tools expect -- only "kr" -> "KR" has
# actually been confirmed live; the rest follow OP.GG's own URL scheme
# (op.gg/lol/summoners/<region>/...) but haven't been individually tested against the real server.
_PLATFORM_REGION_TO_OPGG_REGION = {
    "na1": "NA", "euw1": "EUW", "eun1": "EUNE", "kr": "KR", "jp1": "JP",
    "br1": "BR", "la1": "LAN", "la2": "LAS", "oc1": "OCE", "tr1": "TR", "ru": "RU",
    "ph2": "PH", "sg2": "SG", "th2": "TH", "tw2": "TW", "vn2": "VN",
}


def platform_region_to_opgg_region(platform_region: str) -> str:
    """Falls back to uppercasing the input with a trailing digit stripped if the region isn't in
    the table, rather than raising -- lets an unmapped region at least attempt the call instead
    of hard-failing before it even tries."""
    if platform_region in _PLATFORM_REGION_TO_OPGG_REGION:
        return _PLATFORM_REGION_TO_OPGG_REGION[platform_region]
    return re.sub(r"\d+$", "", platform_region).upper()


async def import_lane_meta(conn) -> dict:
    """Fetches all 5 lanes' win/pick/ban/tier from lol_list_lane_meta_champions and upserts into
    global_tier_list (source_note="opgg-mcp"), resolving each champion's OP.GG display name
    against the champions table. A champion name that doesn't resolve (e.g. brand new, not yet
    DDragon-synced) is skipped and counted, not fatal to the rest of the import.

    Also populates champion_role_eligibility (source "opgg-mcp") from each row's games_played --
    found live, the hard way, during this prototype's own smoke test: without this, a champion
    with ONLY opgg-mcp tier-list data (no personal/pro/REST-opgg eligibility row) resolves to
    role=None in resolve_pick_roles/get_champion_detail, which blanks out not just ban_rate but
    win_rate/pick_rate too (both keyed by role) AND leaves role-gated suggestion filtering unable
    to recognize the champion's role at all. This mirrors the exact fix already applied to the
    REST-scrape path (ingest/tier_list.py:import_from_opgg) for the identical failure mode,
    discovered independently earlier the same day."""
    async with opgg_mcp_client.opgg_session() as session:
        rows = await opgg_mcp_client.list_lane_meta_champions(session, position="all")

    # This tool doesn't return a patch version -- reuse whatever the REST-scrape tier list (or
    # manual YAML import) already established, so opgg-mcp rows land on the same patch key rather
    # than fragmenting global_tier_list across two different "current patch" values.
    patch = tierlist_repo.get_latest_patch(conn) or "unknown"

    imported = 0
    unresolved: list[str] = []
    eligibility_rows: list[tuple[int, str, int]] = []
    for row in rows:
        champ = champion_repo.get_champion_by_name(conn, row.champion_name)
        if champ is None:
            unresolved.append(row.champion_name)
            continue
        tierlist_repo.upsert_tier_entry(
            conn, champion_id=champ["champion_id"], role=row.lane, patch=patch,
            win_rate=row.win_rate, pick_rate=row.pick_rate, ban_rate=row.ban_rate,
            tier=str(row.tier) if row.tier is not None else None, sample_size=row.games_played,
            source_note="opgg-mcp",
        )
        imported += 1
        if row.games_played:
            eligibility_rows.append((champ["champion_id"], row.lane, row.games_played))

    # Always called, even with an empty list -- a run that (this time) resolves zero champions
    # correctly wipes any stale "opgg-mcp" eligibility rows from a previous run, matching
    # replace_role_eligibility's documented "wholesale replace per source" contract.
    champion_repo.replace_role_eligibility(conn, "opgg-mcp", eligibility_rows)

    return {
        "source": "opgg-mcp", "patch": patch, "entries_imported": imported,
        "champions_unresolved": sorted(set(unresolved)),
    }


async def import_summoner_ranks(conn, players: list[dict] | None = None) -> dict:
    """One lol_get_summoner_profile call per active roster player (or the given `players` list),
    storing every queue's standing into summoner_rank. Per-player failures (a private profile, a
    typo'd Riot ID, a region mismatch) are isolated and reported, not fatal for the whole batch --
    same philosophy as pre_draft_refresh.py's per-player error isolation."""
    if players is None:
        players = roster_repo.get_active_players(conn)

    results = []
    async with opgg_mcp_client.opgg_session() as session:
        for player in players:
            region = platform_region_to_opgg_region(player["platform_region"])
            try:
                profile = await opgg_mcp_client.get_summoner_profile(
                    session, game_name=player["riot_game_name"], tag_line=player["riot_tag_line"],
                    region=region,
                )
            except opgg_mcp_client.OpggMcpToolError as e:
                logger.warning("get_summoner_profile failed for %s: %s", player["display_name"], e)
                results.append({
                    "player_id": player["player_id"], "display_name": player["display_name"],
                    "status": "error", "error": str(e),
                })
                continue

            entries = [
                {"queue_type": entry.game_type, "tier": entry.tier, "division": entry.division,
                 "lp": entry.lp, "wins": entry.wins, "losses": entry.losses}
                for entry in profile.league_entries if entry.game_type is not None
            ]
            summoner_rank_repo.replace_player_rank(conn, player["player_id"], entries)
            results.append({
                "player_id": player["player_id"], "display_name": player["display_name"],
                "status": "ok", "queues_found": len(entries),
            })

    return {"source": "opgg-mcp", "players": results}


def _distribute_wins(games: int | None, counter_win_rate: float | None) -> tuple[int, int] | None:
    """counter_win_rate is the COUNTER champion's win rate specifically against the analyzed
    champion (confirmed live: querying the same counter champion against two different analyzed
    champions gave two DIFFERENT win_rate values -- e.g. Smolder showed 0.59 vs Ahri but 0.58 vs
    Katarina -- ruling out "counter's own global win rate" and confirming it's matchup-specific).
    Returns (wins_for_analyzed_champion, wins_for_counter) or None if either input is missing."""
    if games is None or counter_win_rate is None:
        return None
    wins_counter = round(counter_win_rate * games)
    return games - wins_counter, wins_counter


async def import_champion_matchups_and_synergies(conn, champion_positions: list[tuple[str, str]]) -> dict:
    """champion_positions: [(champion_display_name, position), ...] -- a caller-provided, BOUNDED
    list, not "every champion x every position" (~850 calls at ~2s each is impractical to run
    casually, and unkind to a free, undocumented, unauthenticated service). See
    api/routers/refresh.py for how the default set is chosen (top-N per lane from the
    just-fetched lane meta).

    Writes matchup_opgg (both (a,b) and (b,a) directions, per synergy_repo.replace_matchup's
    contract) and synergy_opgg. Wholesale-replaces both tables at the end (same convention as
    aggregate/roster_synergy.py's recompute_roster_synergy) so a partial run still leaves a
    consistent, if incomplete, snapshot rather than a half-old-half-new mix. A champion/position
    pair that fails (rate limit, invalid combo) is logged and skipped, not fatal to the rest."""
    matchup_rows: dict[tuple[int, int], tuple[int, int]] = {}   # (a, b) -> (games, wins_a)
    synergy_rows: dict[tuple[int, int], tuple[int, int]] = {}   # canonical (a, b) -> (games, wins)
    unresolved: set[str] = set()
    analyzed = 0

    async with opgg_mcp_client.opgg_session() as session:
        for champion_name, position in champion_positions:
            me = champion_repo.get_champion_by_name(conn, champion_name)
            if me is None:
                unresolved.add(champion_name)
                continue
            try:
                analysis = await opgg_mcp_client.get_champion_analysis(
                    session, champion=champion_name, position=position,
                )
            except opgg_mcp_client.OpggMcpToolError as e:
                logger.warning("get_champion_analysis(%s, %s) failed: %s", champion_name, position, e)
                continue
            analyzed += 1

            for counter in (*analysis.strong_counters, *analysis.weak_counters):
                opponent = champion_repo.get_champion_by_name(conn, counter.champion_name)
                if opponent is None:
                    unresolved.add(counter.champion_name)
                    continue
                split = _distribute_wins(counter.games, counter.win_rate)
                if split is None:
                    continue
                wins_me, wins_opponent = split
                matchup_rows[(me["champion_id"], opponent["champion_id"])] = (counter.games, wins_me)
                matchup_rows[(opponent["champion_id"], me["champion_id"])] = (counter.games, wins_opponent)

            for ally in analysis.synergies:
                mate = champion_repo.get_champion_by_name(conn, ally.ally_champion_name)
                if mate is None:
                    unresolved.add(ally.ally_champion_name)
                    continue
                if mate["champion_id"] == me["champion_id"] or ally.games is None or ally.win_rate is None:
                    continue
                a, b = sorted((me["champion_id"], mate["champion_id"]))
                synergy_rows[(a, b)] = (ally.games, round(ally.win_rate * ally.games))

    synergy_repo.replace_matchup(
        conn, "matchup_opgg", [(a, b, games, wins) for (a, b), (games, wins) in matchup_rows.items()],
    )
    synergy_repo.replace_synergy(
        conn, "synergy_opgg", [(a, b, games, wins) for (a, b), (games, wins) in synergy_rows.items()],
    )

    return {
        "source": "opgg-mcp", "champion_positions_requested": len(champion_positions),
        "champions_analyzed": analyzed, "champions_unresolved": sorted(unresolved),
        "matchup_pairs": len(matchup_rows) // 2, "synergy_pairs": len(synergy_rows),
    }


async def default_champion_positions_from_lane_meta(conn, top_n_per_lane: int = 10) -> list[tuple[str, str]]:
    """Convenience for api/routers/refresh.py: the top N champions per lane (by OP.GG's own rank
    field, i.e. most-relevant/most-played first) from the CURRENTLY STORED global_tier_list
    opgg-mcp rows -- meant to be called after import_lane_meta, not standalone. Bounds the
    otherwise-impractical full champion x position matrix to the champions actually worth having
    counter/synergy data for."""
    patch = tierlist_repo.get_latest_patch(conn)
    if patch is None:
        return []
    pairs: list[tuple[str, str]] = []
    for role in ("TOP", "JUNGLE", "MID", "BOTTOM", "SUPPORT"):
        entries = [
            e for e in tierlist_repo.get_tier_list_for_patch(conn, patch)
            if e["role"] == role and e.get("source_note") == "opgg-mcp"
        ]
        entries.sort(key=lambda e: e.get("sample_size") or 0, reverse=True)
        position = {"TOP": "top", "JUNGLE": "jungle", "MID": "mid", "BOTTOM": "adc", "SUPPORT": "support"}[role]
        for entry in entries[:top_n_per_lane]:
            champ = champion_repo.get_champion_by_id(conn, entry["champion_id"])
            if champ is not None:
                pairs.append((champ["name"], position))
    return pairs
