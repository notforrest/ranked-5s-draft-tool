"""Adaptive, proactive application rate limiter for the Riot API.

Riot enforces per-application rate limits as a set of (limit, window_seconds) buckets, e.g.
"20 requests per 1 second AND 100 requests per 120 seconds" -- a request must fit under EVERY
bucket simultaneously. The exact buckets are not fixed forever: Riot can (and does) change them,
and they're only authoritatively knowable from response headers:

    X-App-Rate-Limit:       "20:1,100:120"   -- limit:window pairs, comma separated
    X-App-Rate-Limit-Count: "1:1,1:120"      -- current usage count for each of those windows

This limiter starts with a conservative *assumption* matching Riot's well-known personal-key
defaults (20 req/1s, 100 req/120s) so it behaves sensibly before the first real response arrives,
then overwrites its bucket definitions from headers on every subsequent response -- never
hardcoded/trusted beyond that first guess.

Design: a real sliding-window implementation per bucket (a deque of request timestamps), not a
fixed sleep. Before issuing a request, `acquire()` blocks only long enough that firing the request
now would not make any tracked bucket's count within its trailing window meet or exceed that
bucket's limit -- i.e. we proactively throttle to stay under the ceiling rather than firing until
a 429 forces us to.
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    limit: int
    window_s: float
    # Timestamps (monotonic clock) of requests counted within this bucket's trailing window.
    timestamps: deque[float] = field(default_factory=deque)

    def prune(self, now: float) -> None:
        cutoff = now - self.window_s
        while self.timestamps and self.timestamps[0] <= cutoff:
            self.timestamps.popleft()

    def wait_time(self, now: float) -> float:
        """How long to wait (seconds) before another request would fit under this bucket's
        limit, given its current contents. 0 if a request could be issued right now."""
        self.prune(now)
        if len(self.timestamps) < self.limit:
            return 0.0
        # Oldest timestamp still in-window falls out of the window at oldest + window_s;
        # only then does another slot free up.
        oldest = self.timestamps[0]
        return max(0.0, (oldest + self.window_s) - now)

    def record(self, now: float) -> None:
        self.prune(now)
        self.timestamps.append(now)


_HEADER_PAIR_RE = re.compile(r"(\d+):(\d+)")


def _parse_header_pairs(value: str) -> list[tuple[int, int]]:
    """Parses "20:1,100:120" -> [(20, 1), (100, 120)]. Tolerant of stray whitespace."""
    return [(int(a), int(b)) for a, b in _HEADER_PAIR_RE.findall(value)]


class AppRateLimiter:
    """Tracks Riot's per-application rate limit buckets and proactively throttles.

    Thread-safe enough for this project's usage pattern (a single-threaded refresh job), guarded
    with a lock anyway since `acquire()` sleeps while holding state that `record()` also mutates.
    """

    # Conservative default assumption before any real response has been seen. Riot's documented
    # personal-key defaults as of this writing; immediately superseded by real header data.
    _DEFAULT_LIMITS = [(20, 1), (100, 120)]

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: list[_Bucket] = [
            _Bucket(limit=limit, window_s=float(window)) for limit, window in self._DEFAULT_LIMITS
        ]

    def acquire(self) -> None:
        """Blocks (sleeps) until issuing a request right now would not meet or exceed any
        tracked bucket's limit within its window. Call this immediately before every real
        (non-cached) network request."""
        while True:
            with self._lock:
                now = time.monotonic()
                wait = max((b.wait_time(now) for b in self._buckets), default=0.0)
                if wait <= 0.0:
                    # Reserve the slot now, under the same lock, so concurrent callers don't
                    # both observe "no wait needed" and both fire.
                    for b in self._buckets:
                        b.record(now)
                    return
            # Sleep outside the lock so other threads can make progress / re-check.
            time.sleep(wait)

    def record(self, headers) -> None:
        """Updates bucket definitions and (implicitly, via acquire's own recording) usage from
        a real response's headers. Safe to call with headers missing the rate-limit keys (e.g.
        an error response from a proxy) -- becomes a no-op in that case.

        Note: `acquire()` already records a timestamp for the request it just permitted, so this
        method's job is specifically to reconcile our *bucket definitions* (limit/window pairs)
        against what Riot's servers actually say they are right now -- since Riot can change them
        without notice, and our own local counters could otherwise drift from server-side truth.
        """
        limit_header = headers.get("X-App-Rate-Limit")
        if not limit_header:
            return
        try:
            new_defs = _parse_header_pairs(limit_header)
        except (ValueError, TypeError):
            return
        if not new_defs:
            return

        count_header = headers.get("X-App-Rate-Limit-Count", "")
        try:
            counts_by_window = {window: count for count, window in _parse_header_pairs(count_header)}
        except (ValueError, TypeError):
            counts_by_window = {}

        with self._lock:
            now = time.monotonic()
            new_buckets: list[_Bucket] = []
            for limit, window in new_defs:
                existing = next((b for b in self._buckets if b.window_s == float(window)), None)
                bucket = existing if existing is not None else _Bucket(limit=limit, window_s=float(window))
                bucket.limit = limit
                bucket.window_s = float(window)

                # Reconcile against Riot's authoritative live count for this window when
                # available: if the server says N requests have landed in this window and we
                # have fewer local timestamps than that (e.g. limiter was just constructed, or
                # another process/run also shares this key), backfill synthetic "now" timestamps
                # so our local throttling doesn't undercount and blow past the real ceiling.
                server_count = counts_by_window.get(window)
                if server_count is not None:
                    bucket.prune(now)
                    if server_count > len(bucket.timestamps):
                        for _ in range(server_count - len(bucket.timestamps)):
                            bucket.timestamps.append(now)

                new_buckets.append(bucket)
            self._buckets = new_buckets
