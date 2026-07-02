"""Tests for riot_client/endpoints.py's match-id pagination: get_all_ranked_match_ids must page
through Match-V5 with type=ranked until a short page or safety_cap, not stop at a single fixed
window -- this is the fix for personal stats undercounting a player's real ranked history."""
from __future__ import annotations

import httpx
import respx

from draftassistant.riot_client import endpoints
from draftassistant.riot_client.client import RiotAPIClient
from draftassistant.riot_client.rate_limiter import AppRateLimiter

PUUID = "fake-puuid-0001"
ACCOUNT_REGION = "americas"
IDS_URL_RE = rf"https://americas\.api\.riotgames\.com/lol/match/v5/matches/by-puuid/{PUUID}/ids.*"


def _client(tmp_path) -> RiotAPIClient:
    return RiotAPIClient(api_key="RGAPI-fake", cache_dir=tmp_path, rate_limiter=AppRateLimiter())


def _ids_page(n: int, offset: int = 0) -> list[str]:
    return [f"NA1_{offset + i}" for i in range(n)]


def test_get_match_ids_includes_start_count_type_params(tmp_path):
    with respx.mock:
        route = respx.get(url__regex=IDS_URL_RE).mock(return_value=httpx.Response(200, json=[]))
        endpoints.get_match_ids(
            _client(tmp_path), PUUID, ACCOUNT_REGION,
            start=200, count=100, start_time_epoch_s=1700000000, match_type="ranked",
        )
    request_url = str(route.calls[0].request.url)
    assert "start=200" in request_url
    assert "count=100" in request_url
    assert "startTime=1700000000" in request_url
    assert "type=ranked" in request_url


def test_get_all_ranked_match_ids_single_short_page_stops_after_one_call(tmp_path):
    """A page shorter than the page size means there's nothing more to fetch -- must not issue a
    second request just to confirm that."""
    with respx.mock:
        route = respx.get(url__regex=IDS_URL_RE).mock(
            return_value=httpx.Response(200, json=_ids_page(3))
        )
        result = endpoints.get_all_ranked_match_ids(
            _client(tmp_path), PUUID, ACCOUNT_REGION, safety_cap=1000,
        )
    assert result == _ids_page(3)
    assert route.call_count == 1


def test_get_all_ranked_match_ids_pages_through_multiple_full_pages(tmp_path):
    """The real bug this fixes: a player's ranked history spanning more than one page (100 ids)
    must all be collected, not just the first page."""
    page1 = _ids_page(100, offset=0)
    page2 = _ids_page(30, offset=100)
    with respx.mock:
        route = respx.get(url__regex=IDS_URL_RE).mock(
            side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
        )
        result = endpoints.get_all_ranked_match_ids(
            _client(tmp_path), PUUID, ACCOUNT_REGION, safety_cap=1000,
        )
    assert result == page1 + page2
    assert route.call_count == 2
    # Second call's start offset must advance by exactly one page size.
    second_call_url = str(route.calls[1].request.url)
    assert "start=100" in second_call_url
    assert "type=ranked" in second_call_url


def test_get_all_ranked_match_ids_stops_at_safety_cap_even_if_more_data_exists(tmp_path):
    """A pathological account with more ranked games than the safety cap must not cause unbounded
    fetching -- and the returned list must be truncated to exactly the cap, not just have the
    fetch loop stop wherever it happens to land."""
    with respx.mock:
        respx.get(url__regex=IDS_URL_RE).mock(
            return_value=httpx.Response(200, json=_ids_page(100))  # always a full page
        )
        result = endpoints.get_all_ranked_match_ids(
            _client(tmp_path), PUUID, ACCOUNT_REGION, safety_cap=250,
        )
    assert len(result) == 250


def test_get_all_ranked_match_ids_forwards_start_time_to_every_page(tmp_path):
    """start_time_epoch_s bounds the whole fetch (e.g. the incremental-refresh watermark) and
    must be present on every page's request, not just the first."""
    page1 = _ids_page(100, offset=0)
    page2 = _ids_page(5, offset=100)
    with respx.mock:
        route = respx.get(url__regex=IDS_URL_RE).mock(
            side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
        )
        endpoints.get_all_ranked_match_ids(
            _client(tmp_path), PUUID, ACCOUNT_REGION,
            start_time_epoch_s=1700000000, safety_cap=1000,
        )
    for call in route.calls:
        assert "startTime=1700000000" in str(call.request.url)
