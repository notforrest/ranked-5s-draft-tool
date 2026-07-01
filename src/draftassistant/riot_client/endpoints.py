"""Thin, typed wrappers around RiotAPIClient.get() for the specific endpoints this project needs.

Retry policy lives HERE (not in client.py): on RiotRateLimitError, sleep retry_after and retry,
up to a bounded number of attempts -- Riot's rate limits are transient and expected to clear
shortly. On RiotAuthError, NEVER retry -- an expired/invalid key will not fix itself by waiting,
so we let it propagate immediately to the caller (pre_draft_refresh.py), which treats it as
fatal for the whole run when it happens on the first player.
"""
from __future__ import annotations

import time

from draftassistant.riot_client.client import RiotAPIClient
from draftassistant.riot_client.exceptions import RiotRateLimitError

_MAX_ATTEMPTS = 5


def _get_with_retry(client: RiotAPIClient, host: str, path: str, *, cache_key: str | None = None) -> dict:
    """Calls client.get(), retrying on RiotRateLimitError up to _MAX_ATTEMPTS times by sleeping
    the server-specified retry_after each time. RiotAuthError/RiotNotFoundError propagate on the
    first occurrence -- no retry, per this module's docstring."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return client.get(host, path, cache_key=cache_key)
        except RiotRateLimitError as e:
            if attempt >= _MAX_ATTEMPTS:
                raise
            time.sleep(e.retry_after)


def resolve_puuid(client: RiotAPIClient, game_name: str, tag_line: str, account_region: str) -> str:
    """Riot ID (gameName#tagLine) -> puuid via Account-V1. Uses REGIONAL routing
    (americas/asia/europe) -- NOT platform routing (na1/euw1/kr/...), which is a common mistake
    since most other endpoints (summoner, mastery) use platform routing instead.

    No cache_key here: a Riot ID's resolved puuid essentially never changes, but permanent
    caching of that fact happens one layer up, in players.puuid (via roster_repo.set_puuid) --
    not as a disk cache in this client, since the *lookup* itself is cheap/infrequent (once per
    new roster member) and callers need a fresh network round trip to notice if a player has
    renamed their Riot ID.
    """
    valid_regions = ("americas", "asia", "europe")
    if account_region not in valid_regions:
        raise ValueError(
            f"account_region must be one of {valid_regions} (regional routing) for Account-V1 -- "
            f"got {account_region!r}. Note this is different from platform routing (na1/euw1/kr/...) "
            f"used by most other Riot endpoints."
        )
    data = _get_with_retry(
        client,
        f"{account_region}.api.riotgames.com",
        f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}",
    )
    return data["puuid"]


def get_champion_mastery(client: RiotAPIClient, puuid: str, platform_region: str) -> list[dict]:
    """Full champion mastery snapshot for a player. Uses PLATFORM routing (na1/euw1/kr/...).
    No cache -- this is a point-in-time snapshot that should always be refetched on refresh."""
    return _get_with_retry(
        client,
        f"{platform_region}.api.riotgames.com",
        f"/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}",
    )


def get_match_ids(
    client: RiotAPIClient,
    puuid: str,
    account_region: str,
    *,
    start_time_epoch_s: int | None = None,
    count: int = 100,
) -> list[str]:
    """List of match ids for a player, newest-relevant window controlled by `count` and
    optionally `start_time_epoch_s` (Match-V5's `startTime` query param, epoch SECONDS). Uses
    REGIONAL routing. No cache -- this list grows over time and must always hit the network."""
    query = f"?count={count}"
    if start_time_epoch_s is not None:
        query += f"&startTime={start_time_epoch_s}"
    return _get_with_retry(
        client,
        f"{account_region}.api.riotgames.com",
        f"/lol/match/v5/matches/by-puuid/{puuid}/ids{query}",
    )


def get_match(client: RiotAPIClient, match_id: str, account_region: str) -> dict:
    """Full match detail. Uses REGIONAL routing. CACHED forever, keyed by match_id -- a completed
    match's detail is immutable, so cache hits short-circuit the network and the rate limiter
    entirely."""
    return _get_with_retry(
        client,
        f"{account_region}.api.riotgames.com",
        f"/lol/match/v5/matches/{match_id}",
        cache_key=match_id,
    )
