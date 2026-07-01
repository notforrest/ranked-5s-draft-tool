"""Tests for AppRateLimiter's header parsing, bucket state updates, and RiotRateLimitError
propagation via the full RiotAPIClient (mocked with respx)."""
from __future__ import annotations

import time

import httpx
import pytest
import respx

from draftassistant.riot_client.client import RiotAPIClient
from draftassistant.riot_client.exceptions import RiotRateLimitError
from draftassistant.riot_client.rate_limiter import AppRateLimiter


def test_default_buckets_before_any_response():
    limiter = AppRateLimiter()
    # Should not block at all for the first request under the conservative default.
    started = time.monotonic()
    limiter.acquire()
    assert time.monotonic() - started < 0.1


def test_record_updates_bucket_definitions_from_headers():
    limiter = AppRateLimiter()
    headers = httpx.Headers({
        "X-App-Rate-Limit": "20:1,100:120",
        "X-App-Rate-Limit-Count": "5:1,17:120",
    })
    limiter.record(headers)

    # Bucket defs should now match exactly what the header said.
    limits = sorted((b.limit, b.window_s) for b in limiter._buckets)
    assert limits == [(20, 1.0), (100, 120.0)]

    # Server-reported counts should be reflected (backfilled) into local bucket state.
    one_s_bucket = next(b for b in limiter._buckets if b.window_s == 1.0)
    hundred_twenty_s_bucket = next(b for b in limiter._buckets if b.window_s == 120.0)
    assert len(one_s_bucket.timestamps) >= 5
    assert len(hundred_twenty_s_bucket.timestamps) >= 17


def test_record_updates_to_a_smaller_limit_and_acquire_blocks_appropriately():
    """If Riot's headers report a much smaller bucket (e.g. 2 req/1s) that's already saturated,
    acquire() must block rather than firing immediately."""
    limiter = AppRateLimiter()
    headers = httpx.Headers({
        "X-App-Rate-Limit": "2:1,100:120",
        "X-App-Rate-Limit-Count": "2:1,2:120",
    })
    limiter.record(headers)

    one_s_bucket = next(b for b in limiter._buckets if b.window_s == 1.0)
    wait = one_s_bucket.wait_time(time.monotonic())
    assert wait > 0
    assert wait <= 1.0


def test_record_ignores_malformed_or_missing_headers():
    limiter = AppRateLimiter()
    original_buckets = list(limiter._buckets)

    limiter.record(httpx.Headers({}))
    assert limiter._buckets == original_buckets or len(limiter._buckets) == len(original_buckets)

    limiter.record(httpx.Headers({"X-App-Rate-Limit": "garbage"}))
    # Should not raise, and should leave sane (parseable) bucket state.
    assert isinstance(limiter._buckets, list)


def test_client_429_raises_rate_limit_error_with_retry_after(tmp_path):
    limiter = AppRateLimiter()
    client = RiotAPIClient(api_key="RGAPI-fake", cache_dir=tmp_path, rate_limiter=limiter)

    with respx.mock:
        respx.get("https://na1.api.riotgames.com/some/path").mock(
            return_value=httpx.Response(
                429,
                headers={
                    "Retry-After": "3",
                    "X-App-Rate-Limit": "20:1,100:120",
                    "X-App-Rate-Limit-Count": "20:1,45:120",
                },
                json={"status": {"message": "Rate limit exceeded", "status_code": 429}},
            )
        )

        with pytest.raises(RiotRateLimitError) as exc_info:
            client.get("na1.api.riotgames.com", "/some/path")

        assert exc_info.value.retry_after == 3.0

    # The 429 response's headers should still have been fed to record() -- bucket defs updated.
    limits = sorted((b.limit, b.window_s) for b in limiter._buckets)
    assert limits == [(20, 1.0), (100, 120.0)]


def test_client_429_default_retry_after_when_header_missing(tmp_path):
    limiter = AppRateLimiter()
    client = RiotAPIClient(api_key="RGAPI-fake", cache_dir=tmp_path, rate_limiter=limiter)

    with respx.mock:
        respx.get("https://na1.api.riotgames.com/some/path").mock(
            return_value=httpx.Response(429, json={})
        )
        with pytest.raises(RiotRateLimitError) as exc_info:
            client.get("na1.api.riotgames.com", "/some/path")
        assert exc_info.value.retry_after == 1.0


def test_client_successful_response_records_headers(tmp_path):
    limiter = AppRateLimiter()
    client = RiotAPIClient(api_key="RGAPI-fake", cache_dir=tmp_path, rate_limiter=limiter)

    with respx.mock:
        respx.get("https://na1.api.riotgames.com/some/path").mock(
            return_value=httpx.Response(
                200,
                headers={
                    "X-App-Rate-Limit": "30:2,200:150",
                    "X-App-Rate-Limit-Count": "1:2,1:150",
                },
                json={"hello": "world"},
            )
        )
        data = client.get("na1.api.riotgames.com", "/some/path")
        assert data == {"hello": "world"}

    limits = sorted((b.limit, b.window_s) for b in limiter._buckets)
    assert limits == [(30, 2.0), (200, 150.0)]


def test_sliding_window_expires_old_requests():
    """A bucket with a short window should free up capacity once enough time has passed --
    verifying this is a real sliding window, not a fixed sleep-once mechanism."""
    limiter = AppRateLimiter()
    bucket = next(b for b in limiter._buckets if b.window_s == 1.0)

    now = time.monotonic()
    # Fill the bucket to its limit at "now".
    for _ in range(bucket.limit):
        bucket.record(now)

    assert bucket.wait_time(now) > 0
    # After the window has fully elapsed, the same bucket should show 0 wait.
    later = now + bucket.window_s + 0.01
    assert bucket.wait_time(later) == 0.0
