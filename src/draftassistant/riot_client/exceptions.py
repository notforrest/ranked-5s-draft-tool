"""Exceptions raised by the Riot API client.

These map directly to HTTP status codes returned by Riot's API:
- 401/403 -> RiotAuthError (key expired/invalid -- never retried, propagates immediately)
- 429     -> RiotRateLimitError (carries retry_after so callers know how long to back off)
- 404     -> RiotNotFoundError (e.g. unresolvable Riot ID, unknown match id)
"""
from __future__ import annotations


class RiotAPIError(Exception):
    """Base class for all Riot API client errors."""


class RiotAuthError(RiotAPIError):
    """Raised on 401/403 -- API key is missing, expired, or invalid.

    No retry will fix this; the caller must get a fresh key. This is the one error class
    the pre-draft refresh job treats as fatal-for-the-whole-run when it hits the FIRST player,
    since a dead key fails identically for everyone.
    """


class RiotRateLimitError(RiotAPIError):
    """Raised on 429 -- rate limit exceeded. Carries how long (seconds) to wait before retrying,
    taken from the Retry-After response header."""

    def __init__(self, retry_after: float, message: str = "Riot API rate limit exceeded"):
        super().__init__(message)
        self.retry_after = retry_after


class RiotNotFoundError(RiotAPIError):
    """Raised on 404 -- the requested resource does not exist (e.g. unresolvable Riot ID,
    unknown match id)."""
