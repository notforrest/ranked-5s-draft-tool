"""OP.GG champion-stats client -- global win/pick rate, unofficial and best-effort.

OP.GG's own help center ("Can I use OP.GG data?") states they do not prohibit non-commercial
crawling/scraping with reasonable request volume -- this is the most permissive stance found
among the major LoL tier-list sites when this was researched (lolalytics explicitly states its
data "may not be used by third parties"; u.gg and League of Graphs are silent/ambiguous on
scraping specifically). This client hits `lol-api-champion.op.gg`, the internal JSON API OP.GG's
own frontend calls -- discovered by reading an open-source reference client
(github.com/atopx/opgg), NOT something OP.GG publishes or supports for third-party use. It is
UNOFFICIAL and can break without notice if OP.GG changes it.

This is why it stays entirely separate from (additive to) the hand-maintained
data/curated/tier_list/*.yaml path (ingest/tier_list.py's import_tier_list_file): if this
breaks, the manual path is unaffected and still works. Riot's own API does not expose win/pick
rate data at all, which is the whole reason either path exists.
"""
from __future__ import annotations

import httpx

_BASE = "https://lol-api-champion.op.gg"
_TIMEOUT_S = 15.0
_USER_AGENT = "ranked-5s-draft-tool/1.0 (personal, non-commercial, low-frequency use)"


def fetch_champion_stats(mode: str = "ranked", hl: str = "en_US") -> dict:
    """Fetches the full champion list for `mode` ("ranked" or "aram") with global win/pick
    rate stats in a single request. Returns the raw parsed JSON: {"meta": {...}, "data": [...]}
    -- one entry per champion in `data`, see ingest/tier_list.py:import_from_opgg for the shape
    consumed from it."""
    resp = httpx.get(
        f"{_BASE}/api/global/champions/{mode}",
        params={"hl": hl},
        timeout=_TIMEOUT_S,
        headers={"User-Agent": _USER_AGENT},
    )
    resp.raise_for_status()
    return resp.json()
