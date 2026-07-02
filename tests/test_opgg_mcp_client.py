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
from mcp.types import CallToolResult, TextContent

from draftassistant.staticdata.opgg_mcp_client import (
    OpggMcpToolError,
    _parse_opgg_class_notation,
    call_tool,
    get_summoner_profile,
    list_lane_meta_champions,
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


class _FakeSession:
    """Stands in for mcp.ClientSession: records the last call_tool() invocation and returns a
    pre-baked CallToolResult instead of hitting the network."""

    def __init__(self, result: CallToolResult):
        self._result = result
        self.last_call = None

    async def call_tool(self, name, arguments):
        self.last_call = (name, arguments)
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
