"""Tests for staticdata/opgg_mcp_client.py.

These test the CLIENT-SIDE logic (result parsing, argument construction, error surfacing)
against a fake session double and real captured server responses -- they do NOT connect to the
live https://mcp-api.op.gg/mcp themselves (that would make CI/test runs depend on network access
and an external service's uptime). The class-notation fixtures below are copy-pasted verbatim
from real live calls made 2026-07-01 while building this prototype (see opgg_mcp_client.py's
module docstring), not synthesized -- so these tests double as a regression check against the
server changing its response format.
"""
from __future__ import annotations

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, ErrorData, TextContent

from draftassistant.staticdata.opgg_mcp_client import (
    OpggMcpToolError,
    _parse_opgg_class_notation,
    call_tool,
    get_champion_analysis,
    get_summoner_profile,
    list_lane_meta_champions,
    list_summoner_matches,
    to_opgg_champion_key,
)

# Captured verbatim from a real lol_get_summoner_profile call against "Hide on bush#KR1"/region=KR.
_REAL_SUMMONER_RESPONSE = (
    "class LolGetSummonerProfile: data\n"
    "class Data: summoner\n"
    "class Summoner: game_name,tagline,level,league_stats\n"
    "class LeagueStat: game_type,win,lose,tier_info\n"
    "class TierInfo: tier,division,lp\n"
    "\n"
    'LolGetSummonerProfile(Data(Summoner("Hide on bush","KR1",918,'
    '[LeagueStat("SOLORANKED",290,240,TierInfo("GRANDMASTER",1,1713)),'
    "LeagueStat(\"FLEXRANKED\",null,null,TierInfo(null,null,null)),"
    'LeagueStat("ARENA",null,null,TierInfo(null,null,null))])))'
)

# Captured verbatim from a real lol_list_lane_meta_champions call, position=mid (truncated to a
# few champions -- the real response has ~50).
_REAL_LANE_META_RESPONSE = (
    "class LolListLaneMetaChampions: data\n"
    "class Data: positions\n"
    "class Positions: mid\n"
    "class Mid: champion,ban_rate,pick_rate,win_rate,tier,kda,play,rank\n"
    "\n"
    "LolListLaneMetaChampions(Data(Positions([Mid(\"Sylas\",0.19,0.09,0.51,1,2.11,126901,1),"
    'Mid("Ahri",0.03,0.09,0.51,1,2.53,127761,2),'
    "Mid(\"Vel'Koz\",0.02,0.01,0.51,3,2.35,15400,37)])))"
)

# Captured verbatim from a real lol_get_champion_analysis call, champion=VELKOZ position=mid --
# this is the exact response that revealed a real bug: positions[] lists EVERY position a
# champion plays (here SUPPORT, MID, ADC for Vel'Koz), not just the requested one, and is not
# ordered to put the requested position first (SUPPORT comes first despite position="mid").
_REAL_CHAMPION_ANALYSIS_RESPONSE = (
    "class LolGetChampionAnalysis: data\n"
    "class Data: summary,strong_counters\n"
    "class Summary: positions\n"
    "class Position: name,stats\n"
    "class Stats: ban_rate,pick_rate,win_rate,tier_data\n"
    "class TierData: tier\n"
    "class StrongCounter: champion_name,win_rate,play\n"
    "\n"
    'LolGetChampionAnalysis(Data(Summary([Position("SUPPORT",Stats(0.02,0.04,0.51,TierData(3))),'
    'Position("MID",Stats(0.02,0.01,0.51,TierData(3))),'
    'Position("ADC",Stats(0.02,0.01,0.53,TierData(4)))]),'
    '[StrongCounter("Cassiopeia",0.63,100),StrongCounter("Irelia",0.6,123),'
    'StrongCounter("Galio",0.58,245)]))'
)

# Captured verbatim from a real lol_get_summoner_profile call requesting champion-pool fields.
_REAL_SUMMONER_WITH_CHAMPION_POOL_RESPONSE = (
    "class LolGetSummonerProfile: data\n"
    "class Data: summoner\n"
    "class Summoner: game_name,tagline,level,ranked_most_champions\n"
    "class RankedMostChampions: my_champion_stats\n"
    "class MyChampionStat: champion_name,play,win,lose\n"
    "\n"
    'LolGetSummonerProfile(Data(Summoner("Hide on bush","KR1",918,'
    "RankedMostChampions([MyChampionStat(\"Aurora\",54,31,23),"
    'MyChampionStat("Anivia",39,28,11),MyChampionStat("Yone",38,18,20)]))))'
)

# Captured verbatim from a real lol_list_summoner_matches call, limit=5 (truncated to 2 games).
_REAL_SUMMONER_MATCHES_RESPONSE = (
    "class LolListSummonerMatches: data\n"
    "class Data: game_history\n"
    "class GameHistory: created_at,game_length_second,game_type,participants,id\n"
    "class Participant: champion_name,position,stats\n"
    "class Stats: assist,death,kill,result\n"
    "\n"
    'LolListSummonerMatches(Data([GameHistory("2026-07-01T02:46:37+09:00",1900,"SOLORANKED",'
    '[Participant("Orianna","MID",Stats(18,5,9,"WIN"))],"abc123="),'
    'GameHistory("2026-07-01T01:41:39+09:00",1611,"SOLORANKED",'
    '[Participant("Anivia","MID",Stats(8,4,2,"WIN"))],"def456=")]))'
)


class _FakeSession:
    """Stands in for mcp.ClientSession: records the last call_tool() invocation and returns a
    pre-baked CallToolResult (or raises a pre-baked exception) instead of hitting the network."""

    def __init__(self, result: CallToolResult | None = None, raises: Exception | None = None):
        self._result = result
        self._raises = raises
        self.last_call = None

    async def call_tool(self, name, arguments):
        self.last_call = (name, arguments)
        if self._raises is not None:
            raise self._raises
        return self._result


def _text_result(text: str, is_error: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=is_error)


def _json_result(payload_dict, is_error=False) -> CallToolResult:
    import json
    return _text_result(json.dumps(payload_dict), is_error=is_error)


# ------------------------------------------------------------------
# _parse_opgg_class_notation -- the server's custom, non-JSON response format
# ------------------------------------------------------------------


def test_parse_class_notation_handles_real_summoner_response():
    parsed = _parse_opgg_class_notation(_REAL_SUMMONER_RESPONSE)
    summoner = parsed["data"]["summoner"]
    assert summoner["game_name"] == "Hide on bush"
    assert summoner["tagline"] == "KR1"
    assert summoner["level"] == 918
    assert len(summoner["league_stats"]) == 3

    solo = summoner["league_stats"][0]
    assert solo["game_type"] == "SOLORANKED"
    assert solo["win"] == 290
    assert solo["lose"] == 240
    assert solo["tier_info"] == {"tier": "GRANDMASTER", "division": 1, "lp": 1713}

    # "null" (JS/JSON token, not Python's None) must be normalized correctly.
    flex = summoner["league_stats"][1]
    assert flex["win"] is None
    assert flex["tier_info"] == {"tier": None, "division": None, "lp": None}


def test_parse_class_notation_handles_real_lane_meta_response():
    parsed = _parse_opgg_class_notation(_REAL_LANE_META_RESPONSE)
    mid = parsed["data"]["positions"]["mid"]
    assert len(mid) == 3
    assert mid[0] == {
        "champion": "Sylas", "ban_rate": 0.19, "pick_rate": 0.09, "win_rate": 0.51,
        "tier": 1, "kda": 2.11, "play": 126901, "rank": 1,
    }
    # A champion name containing an apostrophe (no escaping needed since the string itself is
    # double-quoted) must parse correctly, not be mistaken for a string terminator.
    assert mid[2]["champion"] == "Vel'Koz"


def test_parse_class_notation_rejects_mismatched_arg_count():
    """If the server's response ever adds/removes a field without the header agreeing, this must
    raise loudly rather than silently mis-map fields to the wrong values."""
    bad = (
        "class Foo: a,b,c\n"
        "\n"
        "Foo(1,2)"
    )
    with pytest.raises(ValueError, match="declared 3 field"):
        _parse_opgg_class_notation(bad)


def test_parse_class_notation_rejects_unknown_class():
    bad = "class Foo: a\n\nBar(1)"
    with pytest.raises(ValueError, match="no header declaration"):
        _parse_opgg_class_notation(bad)


# ------------------------------------------------------------------
# call_tool -- generic invocation, error handling, format fallback chain
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_tool_parses_structured_content_when_present():
    result = CallToolResult(content=[], structuredContent={"foo": "bar"}, isError=False)
    session = _FakeSession(result)
    parsed = await call_tool(session, "some_tool", {"a": 1})
    assert parsed == {"foo": "bar"}
    assert session.last_call == ("some_tool", {"a": 1})


@pytest.mark.asyncio
async def test_call_tool_parses_plain_json_when_present():
    session = _FakeSession(_json_result({"foo": "bar"}))
    parsed = await call_tool(session, "some_tool", {})
    assert parsed == {"foo": "bar"}


@pytest.mark.asyncio
async def test_call_tool_parses_class_notation_when_not_json():
    session = _FakeSession(_text_result(_REAL_SUMMONER_RESPONSE))
    parsed = await call_tool(session, "lol_get_summoner_profile", {})
    assert parsed["data"]["summoner"]["game_name"] == "Hide on bush"


@pytest.mark.asyncio
async def test_call_tool_falls_back_to_raw_text_when_unparseable():
    session = _FakeSession(_text_result("completely unstructured error message"))
    parsed = await call_tool(session, "some_tool", {})
    assert parsed == "completely unstructured error message"


@pytest.mark.asyncio
async def test_call_tool_raises_on_server_reported_error():
    session = _FakeSession(_json_result({"message": "unknown region"}, is_error=True))
    with pytest.raises(OpggMcpToolError) as exc_info:
        await call_tool(session, "some_tool", {"region": "MARS"})
    assert exc_info.value.tool_name == "some_tool"
    assert exc_info.value.payload == {"message": "unknown region"}


@pytest.mark.asyncio
async def test_call_tool_normalizes_hard_mcp_error_to_opgg_error():
    """Real bug found during live smoke-testing: a nonexistent Riot ID makes the server raise a
    hard JSON-RPC-level error (mcp.shared.exceptions.McpError, e.g. "Summoner not found") from
    session.call_tool() itself -- BEFORE a CallToolResult even exists -- not a soft
    isError=True result. A caller that only catches OpggMcpToolError (e.g.
    ingest/opgg_mcp_ingest.py's per-player error isolation) would previously see this raw McpError
    propagate uncaught instead. Both failure modes must normalize to the same exception type."""
    session = _FakeSession(raises=McpError(ErrorData(code=-32000, message="Summoner not found")))
    with pytest.raises(OpggMcpToolError) as exc_info:
        await call_tool(session, "lol_get_summoner_profile", {"game_name": "NotARealPlayer"})
    assert exc_info.value.tool_name == "lol_get_summoner_profile"
    assert "Summoner not found" in str(exc_info.value.payload)


# ------------------------------------------------------------------
# get_summoner_profile
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_summoner_profile_builds_expected_arguments_and_calls_right_tool():
    session = _FakeSession(_text_result(_REAL_SUMMONER_RESPONSE))
    await get_summoner_profile(session, game_name="Hide on bush", tag_line="KR1", region="KR")

    name, arguments = session.last_call
    assert name == "lol_get_summoner_profile"
    assert arguments["game_name"] == "Hide on bush"
    assert arguments["tag_line"] == "KR1"
    assert arguments["region"] == "KR"
    assert arguments["lang"] == "en_US"
    assert "desired_output_fields" in arguments


@pytest.mark.asyncio
async def test_get_summoner_profile_parses_real_captured_response():
    session = _FakeSession(_text_result(_REAL_SUMMONER_RESPONSE))
    profile = await get_summoner_profile(session, game_name="Hide on bush", tag_line="KR1", region="KR")

    assert profile.game_name == "Hide on bush"
    assert profile.tagline == "KR1"
    assert profile.level == 918
    assert len(profile.league_entries) == 3

    solo = profile.league_entries[0]
    assert solo.game_type == "SOLORANKED"
    assert solo.tier == "GRANDMASTER"
    assert solo.division == 1
    assert solo.lp == 1713
    assert solo.wins == 290
    assert solo.losses == 240

    # An unplayed queue still shows up as an entry, entirely null -- not omitted.
    flex = profile.league_entries[1]
    assert flex.game_type == "FLEXRANKED"
    assert flex.wins is None
    assert flex.tier is None


@pytest.mark.asyncio
async def test_get_summoner_profile_extra_arguments_override_and_extend():
    session = _FakeSession(_text_result(_REAL_SUMMONER_RESPONSE))
    await get_summoner_profile(
        session, game_name="Hide on bush", tag_line="KR1", region="KR", lang="ko_KR", extra_flag=True,
    )
    _, arguments = session.last_call
    assert arguments["lang"] == "ko_KR"
    assert arguments["extra_flag"] is True


@pytest.mark.asyncio
async def test_get_summoner_profile_degrades_gracefully_on_missing_fields():
    session = _FakeSession(_json_result({}))
    profile = await get_summoner_profile(session, game_name="X", tag_line="Y", region="NA")
    assert profile.game_name is None
    assert profile.league_entries == []


# ------------------------------------------------------------------
# list_lane_meta_champions
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_lane_meta_champions_builds_expected_arguments():
    session = _FakeSession(_json_result({"data": {"positions": {}}}))
    await list_lane_meta_champions(session, position="mid")

    name, arguments = session.last_call
    assert name == "lol_list_lane_meta_champions"
    assert arguments["position"] == "mid"
    assert arguments["lang"] == "en_US"
    # Always requests all 5 lanes regardless of the position filter (see docstring).
    assert len(arguments["desired_output_fields"]) == 5


@pytest.mark.asyncio
async def test_list_lane_meta_champions_parses_real_captured_response():
    session = _FakeSession(_text_result(_REAL_LANE_META_RESPONSE))
    rows = await list_lane_meta_champions(session, position="mid")

    assert len(rows) == 3
    sylas = rows[0]
    assert sylas.champion_name == "Sylas"
    assert sylas.lane == "MID"  # OP.GG's lowercase "mid" mapped to this project's role vocabulary
    assert sylas.ban_rate == 0.19
    assert sylas.pick_rate == 0.09
    assert sylas.win_rate == 0.51
    assert sylas.tier == 1
    assert sylas.kda == 2.11
    assert sylas.games_played == 126901
    assert sylas.rank == 1


@pytest.mark.asyncio
async def test_list_lane_meta_champions_maps_adc_to_bottom():
    payload = {"data": {"positions": {"adc": [
        {"champion": "Jinx", "ban_rate": 0.05, "pick_rate": 0.1, "win_rate": 0.5, "tier": 2,
         "kda": 2.5, "play": 1000, "rank": 1},
    ]}}}
    session = _FakeSession(_json_result(payload))
    rows = await list_lane_meta_champions(session)
    assert rows[0].lane == "BOTTOM"


@pytest.mark.asyncio
async def test_list_lane_meta_champions_raises_opgg_error_through_to_caller():
    session = _FakeSession(_text_result("bad position value", is_error=True))
    with pytest.raises(OpggMcpToolError):
        await list_lane_meta_champions(session, position="not_a_real_lane")


# ------------------------------------------------------------------
# get_summoner_profile -- champion pool
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_summoner_profile_parses_champion_pool():
    session = _FakeSession(_text_result(_REAL_SUMMONER_WITH_CHAMPION_POOL_RESPONSE))
    profile = await get_summoner_profile(session, game_name="Hide on bush", tag_line="KR1", region="KR")

    assert len(profile.champion_pool) == 3
    top = profile.champion_pool[0]
    assert top.champion_name == "Aurora"
    assert top.games == 54
    assert top.wins == 31
    assert top.losses == 23


@pytest.mark.asyncio
async def test_get_summoner_profile_champion_pool_empty_when_absent():
    """The original (pre-champion-pool) fixture has no ranked_most_champions key at all --
    must degrade to an empty list, not KeyError."""
    session = _FakeSession(_text_result(_REAL_SUMMONER_RESPONSE))
    profile = await get_summoner_profile(session, game_name="Hide on bush", tag_line="KR1", region="KR")
    assert profile.champion_pool == []


# ------------------------------------------------------------------
# to_opgg_champion_key
# ------------------------------------------------------------------


@pytest.mark.parametrize("display_name,expected", [
    ("Ahri", "AHRI"),
    ("Vel'Koz", "VELKOZ"),
    ("Twisted Fate", "TWISTEDFATE"),
    ("Kai'Sa", "KAISA"),
    ("Dr. Mundo", "DRMUNDO"),
    ("Wukong", "WUKONG"),
])
def test_to_opgg_champion_key(display_name, expected):
    assert to_opgg_champion_key(display_name) == expected


# ------------------------------------------------------------------
# get_champion_analysis
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_champion_analysis_builds_expected_arguments():
    session = _FakeSession(_json_result({"data": {"summary": {"positions": []}}}))
    await get_champion_analysis(session, champion="Vel'Koz", position="mid")

    name, arguments = session.last_call
    assert name == "lol_get_champion_analysis"
    assert arguments["champion"] == "VELKOZ"
    assert arguments["position"] == "mid"
    assert arguments["game_mode"] == "ranked"
    assert "desired_output_fields" in arguments


@pytest.mark.asyncio
async def test_get_champion_analysis_matches_requested_position_not_first_entry():
    """The real bug this test guards against: positions[] lists every position the champion
    plays (SUPPORT, MID, ADC for Vel'Koz) with SUPPORT first even though position="mid" was
    requested -- the wrapper must find "MID" by name, not blindly take positions[0]."""
    session = _FakeSession(_text_result(_REAL_CHAMPION_ANALYSIS_RESPONSE))
    analysis = await get_champion_analysis(session, champion="Vel'Koz", position="mid")

    assert analysis.position == "MID"
    assert analysis.win_rate == 0.51
    assert analysis.pick_rate == 0.01  # MID's pick_rate, NOT SUPPORT's (0.04) or ADC's (0.01... coincidentally same)
    assert analysis.tier == 3


@pytest.mark.asyncio
async def test_get_champion_analysis_matches_adc_position_correctly():
    """ADC has a distinctly different win_rate/tier than MID in the fixture (0.53/4 vs 0.51/3) --
    a strong signal this test would fail if the wrong position's stats leaked through."""
    session = _FakeSession(_text_result(_REAL_CHAMPION_ANALYSIS_RESPONSE))
    analysis = await get_champion_analysis(session, champion="Vel'Koz", position="adc")

    assert analysis.position == "BOTTOM"  # this project's role vocabulary, not OP.GG's "ADC"
    assert analysis.win_rate == 0.53
    assert analysis.tier == 4


@pytest.mark.asyncio
async def test_get_champion_analysis_parses_strong_counters():
    session = _FakeSession(_text_result(_REAL_CHAMPION_ANALYSIS_RESPONSE))
    analysis = await get_champion_analysis(session, champion="Vel'Koz", position="mid")

    assert len(analysis.strong_counters) == 3
    assert analysis.strong_counters[0].champion_name == "Cassiopeia"
    assert analysis.strong_counters[0].win_rate == 0.63
    assert analysis.strong_counters[0].games == 100


@pytest.mark.asyncio
async def test_get_champion_analysis_degrades_gracefully_when_position_not_found():
    """Defensive case: if the requested position genuinely isn't in the champion's position
    list (e.g. a champion that's never played jungle), must return Nones, not KeyError/IndexError."""
    session = _FakeSession(_text_result(_REAL_CHAMPION_ANALYSIS_RESPONSE))
    analysis = await get_champion_analysis(session, champion="Vel'Koz", position="jungle")

    assert analysis.win_rate is None
    assert analysis.tier is None


@pytest.mark.asyncio
async def test_get_champion_analysis_parses_synergies_by_position():
    payload = {
        "data": {
            "summary": {"positions": [{"name": "MID", "stats": {}}]},
            "synergies": {
                "support": [{"synergy_champion_name": "Thresh", "win_rate": 0.55, "play": 200}],
                "top": [{"synergy_champion_name": "Ornn", "win_rate": 0.52, "play": 150}],
            },
        },
    }
    session = _FakeSession(_json_result(payload))
    analysis = await get_champion_analysis(session, champion="Ahri", position="mid")

    assert len(analysis.synergies) == 2
    support_synergy = next(s for s in analysis.synergies if s.ally_position == "SUPPORT")
    assert support_synergy.ally_champion_name == "Thresh"
    top_synergy = next(s for s in analysis.synergies if s.ally_position == "TOP")
    assert top_synergy.ally_champion_name == "Ornn"


@pytest.mark.asyncio
async def test_get_champion_analysis_raises_on_invalid_position():
    """Confirmed live: the server rejects position="all" even though it's in the input schema's
    enum -- the client must pass through whatever the server says, not silently coerce it."""
    session = _FakeSession(_text_result('{"position":["The selected position is invalid."]}', is_error=True))
    with pytest.raises(OpggMcpToolError):
        await get_champion_analysis(session, champion="Ahri", position="all")


# ------------------------------------------------------------------
# list_summoner_matches
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_summoner_matches_builds_expected_arguments():
    session = _FakeSession(_json_result({"data": {"game_history": []}}))
    await list_summoner_matches(session, game_name="Hide on bush", tag_line="KR1", region="KR", limit=5)

    name, arguments = session.last_call
    assert name == "lol_list_summoner_matches"
    assert arguments["game_name"] == "Hide on bush"
    assert arguments["tag_line"] == "KR1"
    assert arguments["region"] == "KR"
    assert arguments["limit"] == 5
    assert "desired_output_fields" in arguments


@pytest.mark.asyncio
async def test_list_summoner_matches_parses_real_captured_response():
    session = _FakeSession(_text_result(_REAL_SUMMONER_MATCHES_RESPONSE))
    matches = await list_summoner_matches(session, game_name="Hide on bush", tag_line="KR1", region="KR")

    assert len(matches) == 2
    first = matches[0]
    assert first.game_type == "SOLORANKED"
    assert first.champion_name == "Orianna"
    assert first.result == "WIN"
    assert first.kills == 9
    assert first.deaths == 5
    assert first.assists == 18
    assert first.game_length_second == 1900


@pytest.mark.asyncio
async def test_list_summoner_matches_degrades_gracefully_on_empty_history():
    session = _FakeSession(_json_result({"data": {"game_history": []}}))
    matches = await list_summoner_matches(session, game_name="X", tag_line="Y", region="NA")
    assert matches == []
