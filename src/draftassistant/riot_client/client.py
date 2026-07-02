"""Low-level Riot API HTTP client: auth header, disk caching, rate-limiter integration, and
mapping of non-2xx responses to our exception classes.

Deliberately has NO retry logic -- that belongs one layer up in endpoints.py, so this class's
error paths (a 429 raises immediately, a 401 raises immediately) are trivial to unit test in
isolation without needing to also mock away sleeps/retries.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from draftassistant.riot_client.exceptions import RiotAuthError, RiotNotFoundError, RiotRateLimitError
from draftassistant.riot_client.rate_limiter import AppRateLimiter

logger = logging.getLogger(__name__)

_TIMEOUT_S = 10.0


class RiotAPIClient:
    def __init__(self, api_key: str, cache_dir: Path, rate_limiter: AppRateLimiter):
        self._api_key = api_key
        self._cache_dir = Path(cache_dir)
        self._limiter = rate_limiter

    def get(self, host: str, path: str, *, cache_key: str | None = None) -> dict:
        """Issues a GET to https://{host}{path}.

        If `cache_key` is given and a cached response already exists on disk, returns it with
        zero network call and zero rate-limiter interaction (cache hits are free -- they must
        never count against the app's request budget). Otherwise makes a real call, going
        through the rate limiter's acquire()/record() around it, and if `cache_key` is given,
        writes the successful response to disk before returning.
        """
        if cache_key is not None:
            cached = self._read_cache(cache_key)
            if cached is not None:
                logger.info("  (cached) %s", path)
                return cached

        logger.info("  GET %s", path)
        self._limiter.acquire()
        resp = httpx.get(
            f"https://{host}{path}",
            headers={"X-Riot-Token": self._api_key},
            timeout=_TIMEOUT_S,
        )
        self._limiter.record(resp.headers)

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "1"))
            raise RiotRateLimitError(retry_after=retry_after)
        if resp.status_code in (401, 403):
            raise RiotAuthError(
                f"Riot API key rejected (HTTP {resp.status_code}) -- it may be expired or invalid. "
                f"Paste a fresh key into .env."
            )
        if resp.status_code == 404:
            raise RiotNotFoundError(f"Riot API resource not found: {host}{path}")
        resp.raise_for_status()

        data = resp.json()
        if cache_key is not None:
            self._write_cache(cache_key, data)
        return data

    def _cache_path(self, cache_key: str) -> Path:
        return self._cache_dir / f"{cache_key}.json"

    def _read_cache(self, cache_key: str) -> dict | None:
        path = self._cache_path(cache_key)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def _write_cache(self, cache_key: str, data: dict) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path(cache_key).write_text(json.dumps(data))
