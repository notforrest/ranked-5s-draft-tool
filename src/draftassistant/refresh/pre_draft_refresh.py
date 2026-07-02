"""The pre-draft refresh orchestrator -- the ONLY place in this codebase that calls the Riot API.

Run manually before queuing up (scripts/run_pre_draft_refresh.py, or a setup-screen button calling
this same function). Never called from the live draft screen's request path -- that architectural
boundary is what makes an expired key / Riot downtime harmless during an actual draft.
"""
from __future__ import annotations

import json
import logging

from draftassistant import config
from draftassistant.aggregate.personal_stats import recompute_personal_stats
from draftassistant.aggregate.roster_synergy import recompute_roster_synergy
from draftassistant.db.repositories import mastery_repo, match_repo, roster_repo
from draftassistant.riot_client import endpoints
from draftassistant.riot_client.client import RiotAPIClient
from draftassistant.riot_client.exceptions import RiotAuthError, RiotRateLimitError
from draftassistant.riot_client.rate_limiter import AppRateLimiter

logger = logging.getLogger(__name__)


def run(conn) -> dict:
    """Runs the full pre-draft refresh for every active roster player and returns a summary dict.

    Per-player errors are isolated (see _refresh_one_player) except for the specific case of the
    FIRST player hitting a RiotAuthError, which short-circuits the entire run immediately -- a
    dead key fails identically for every player, so there's no point burning rate-limit budget
    (or wall-clock time) rediscovering that once per roster member.
    """
    config.ensure_data_dirs()
    run_id = match_repo.start_refresh_run(conn)

    client = RiotAPIClient(
        api_key=config.RIOT_API_KEY,
        cache_dir=config.RAW_MATCHES_DIR,
        rate_limiter=AppRateLimiter(),
    )

    players = roster_repo.get_active_players(conn)
    logger.info("Starting pre-draft refresh for %d active player(s).", len(players))
    player_summaries: list[dict] = []
    fatal_auth_error: str | None = None

    for i, player in enumerate(players):
        logger.info("[%d/%d] %s (%s#%s)", i + 1, len(players),
                    player["display_name"], player["riot_game_name"], player["riot_tag_line"])
        try:
            summary = _refresh_one_player(conn, client, player)
            player_summaries.append(summary)
        except RiotAuthError as e:
            player_summaries.append({
                "player_id": player["player_id"], "display_name": player["display_name"],
                "status": "error", "error": str(e),
            })
            match_repo.upsert_refresh_state(
                conn, player["player_id"], last_match_fetched_ms=None,
                status="error", error_message=str(e),
            )
            if i == 0:
                # First player's key check failed -- stop immediately, don't touch remaining
                # players at all.
                fatal_auth_error = str(e)
                break
            continue
        except RiotRateLimitError as e:
            msg = f"Rate limited after exhausting retries: {e}"
            player_summaries.append({
                "player_id": player["player_id"], "display_name": player["display_name"],
                "status": "partial", "error": msg,
            })
            match_repo.upsert_refresh_state(
                conn, player["player_id"], last_match_fetched_ms=None,
                status="partial", error_message=msg,
            )
            continue
        except Exception as e:
            # Anything else unexpected (a bad DB row, an integrity error, a malformed API
            # response) shouldn't take down the whole batch -- one player's problem is isolated
            # and clearly reported, same as the Riot-specific error cases above, while everyone
            # else still gets refreshed. Logged with a full traceback for debugging.
            msg = f"Unexpected error: {e}"
            logger.exception("Unexpected error refreshing %s (player_id=%s)",
                              player["display_name"], player["player_id"])
            player_summaries.append({
                "player_id": player["player_id"], "display_name": player["display_name"],
                "status": "error", "error": msg,
            })
            match_repo.upsert_refresh_state(
                conn, player["player_id"], last_match_fetched_ms=None,
                status="error", error_message=msg,
            )
            continue

    # Feed newly-fetched matches into derived stats immediately, even if the run ended early --
    # whatever we did fetch should still be reflected.
    logger.info("Recomputing personal stats and roster synergy...")
    recompute_personal_stats(conn)
    recompute_roster_synergy(conn)

    if fatal_auth_error is not None:
        status = "failed"
    elif any(p.get("status") in ("error", "partial") for p in player_summaries):
        status = "partial"
    else:
        status = "success"

    logger.info("Refresh run #%d finished: %s", run_id, status)

    summary = {
        "run_id": run_id,
        "status": status,
        "players": player_summaries,
        "fatal_auth_error": fatal_auth_error,
    }
    match_repo.finish_refresh_run(conn, run_id, status=status, summary_json=json.dumps(summary))
    return summary


def _refresh_one_player(conn, client: RiotAPIClient, player: dict) -> dict:
    player_id = player["player_id"]

    puuid = player["puuid"]
    if not puuid:
        logger.info("  resolving puuid for Riot ID %s#%s...", player["riot_game_name"], player["riot_tag_line"])
        puuid = endpoints.resolve_puuid(
            client, player["riot_game_name"], player["riot_tag_line"], player["account_region"],
        )
        roster_repo.set_puuid(conn, player_id, puuid)
        logger.info("  resolved puuid.")

    logger.info("  fetching champion mastery...")
    mastery_entries = endpoints.get_champion_mastery(client, puuid, player["platform_region"])
    mastery_repo.replace_player_mastery(conn, player_id, mastery_entries)
    logger.info("  fetched mastery for %d champion(s).", len(mastery_entries))

    match_summary = _refresh_match_history(conn, client, player, puuid)

    return {
        "player_id": player_id,
        "display_name": player["display_name"],
        "status": "ok",
        "puuid": puuid,
        "mastery_champions": len(mastery_entries),
        **match_summary,
    }


def _refresh_match_history(conn, client: RiotAPIClient, player: dict, puuid: str) -> dict:
    """Fetches ranked match ids since whichever is more recent of this player's stored watermark
    or config.CURRENT_SEASON_START_EPOCH_MS (for a first-time player with no watermark, that floor
    IS the season start) via endpoints.get_all_ranked_match_ids, which pages through Match-V5 with
    type=ranked rather than a single fixed-size window -- a first-time backfill no longer risks
    non-ranked games crowding ranked ones out of a small window, and never reaches back into a
    prior season regardless of config.MATCH_HISTORY_SAFETY_CAP (both faster -- Riot's own
    startTime filtering means far fewer pages -- and correct, since prior-season form isn't
    "current" for pick/ban purposes).

    Writes each new match's raw JSON to disk, its metadata to `matches`, and inserts a
    match_participants row ONLY for participants whose puuid belongs to our roster -- looked up
    once up front, not per participant.
    """
    player_id = player["player_id"]
    account_region = player["account_region"]

    refresh_state = match_repo.get_refresh_state(conn, player_id)
    watermark_ms = refresh_state["last_match_fetched_ms"] if refresh_state else None
    effective_start_ms = max(watermark_ms or 0, config.CURRENT_SEASON_START_EPOCH_MS)

    if watermark_ms is None:
        logger.info("  no watermark yet -- backfilling this season's ranked history from %s (up to %d matches)...",
                    config.CURRENT_SEASON_START_DATE, config.MATCH_HISTORY_SAFETY_CAP)
    else:
        logger.info("  fetching ranked matches since %d...", effective_start_ms)

    match_ids = endpoints.get_all_ranked_match_ids(
        client, puuid, account_region,
        start_time_epoch_s=effective_start_ms // 1000,
        safety_cap=config.MATCH_HISTORY_SAFETY_CAP,
    )

    roster_by_puuid = {p["puuid"]: p["player_id"] for p in roster_repo.get_active_players(conn) if p["puuid"]}

    to_fetch = [m for m in match_ids if not match_repo.match_exists(conn, m)]
    logger.info("  %d ranked match id(s) seen, %d already cached, %d new to fetch.",
                len(match_ids), len(match_ids) - len(to_fetch), len(to_fetch))

    new_matches = 0
    max_creation_seen = watermark_ms
    for match_id in match_ids:
        if match_repo.match_exists(conn, match_id):
            continue

        logger.info("  fetching match detail (%d/%d): %s", new_matches + 1, len(to_fetch), match_id)
        match_data = endpoints.get_match(client, match_id, account_region)
        info = match_data["info"]

        raw_path = config.RAW_MATCHES_DIR / f"{match_id}.json"
        raw_path.write_text(json.dumps(match_data))

        game_creation_ms = info["gameCreation"]
        patch = _derive_patch(info.get("gameVersion", ""))

        match_repo.insert_match(
            conn,
            match_id=match_id,
            platform_region=player["platform_region"],
            game_creation_ms=game_creation_ms,
            game_duration_s=info.get("gameDuration"),
            queue_id=info.get("queueId"),
            game_mode=info.get("gameMode"),
            patch=patch,
            raw_json_path=str(raw_path),
        )

        for participant in info.get("participants", []):
            p_puuid = participant.get("puuid")
            if p_puuid not in roster_by_puuid:
                continue
            match_repo.insert_participant(
                conn,
                match_id=match_id,
                puuid=p_puuid,
                player_id=roster_by_puuid[p_puuid],
                champion_id=participant.get("championId"),
                role=_normalize_role(participant),
                win=bool(participant.get("win")),
                kills=participant.get("kills"),
                deaths=participant.get("deaths"),
                assists=participant.get("assists"),
                team_id=participant.get("teamId"),
            )

        new_matches += 1
        if max_creation_seen is None or game_creation_ms > max_creation_seen:
            max_creation_seen = game_creation_ms

    match_repo.upsert_refresh_state(
        conn, player_id, last_match_fetched_ms=max_creation_seen, status="ok",
    )
    logger.info("  done: %d new match(es) fetched, watermark now %s.", new_matches, max_creation_seen)

    return {"new_matches_fetched": new_matches, "match_ids_seen": len(match_ids)}


def _derive_patch(game_version: str) -> str | None:
    """"14.13.123.456" -> "14.13" (major.minor, matching global_tier_list.patch's format)."""
    parts = game_version.split(".")
    if len(parts) < 2:
        return None
    return f"{parts[0]}.{parts[1]}"


def _normalize_role(participant: dict) -> str | None:
    """Match-V5 participants carry `teamPosition` (TOP/JUNGLE/MIDDLE/BOTTOM/UTILITY) -- map to
    this project's TOP/JUNGLE/MID/BOTTOM/SUPPORT vocabulary."""
    position = participant.get("teamPosition") or ""
    return {
        "TOP": "TOP",
        "JUNGLE": "JUNGLE",
        "MIDDLE": "MID",
        "BOTTOM": "BOTTOM",
        "UTILITY": "SUPPORT",
    }.get(position)
