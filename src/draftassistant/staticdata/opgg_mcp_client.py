"""OP.GG MCP (Model Context Protocol) client -- PROTOTYPE, live-verified 2026-07-01.

OP.GG hosts an official, free, no-auth-required MCP server at https://mcp-api.op.gg/mcp
(Streamable HTTP transport) exposing League of Legends/TFT/Valorant tools -- including two of
interest for this project that the REST scrape in opgg_client.py cannot provide:
  - lol_get_summoner_profile: rank/tier/LP/win-rate/champion-pool for a given Riot ID. Nothing
    in this codebase currently has rank/LP at all (no Riot League-V4 API scope is wired up).
  - lol_list_lane_meta_champions: lane-by-lane win/pick/**ban** rate + tier. import_from_opgg's
    REST scrape has no ban rate in its response at all (see ingest/tier_list.py); this one does.

VERIFICATION STATUS: unlike every other op.gg domain (blocked from the machine this was
originally built on -- see project memory), mcp-api.op.gg turned out to be reachable, so this
was iterated against the REAL live server, not guessed blind. Confirmed live 2026-07-01:
  - Both tool names, both tools' exact input JSON Schemas (via scripts/explore_opgg_mcp.py).
  - lol_get_summoner_profile really does return rank/LP for a real Riot ID (spot-checked against
    "Hide on bush#KR1" -- returned GRANDMASTER, 1713 LP, 290W/240L, matching a real high-elo
    account).
  - lol_list_lane_meta_champions really does include ban_rate (spot-checked MID: e.g. Naafiri
    29% ban rate, Locke 64%) -- this is the fix for the ban-rate gap the whole prototype exists
    to close.
  - The server's response format is NOT JSON. It's a custom, compact, positional notation
    designed for LLM token efficiency: a header of `class Name: field1,field2` declarations
    (mapping each nested type to its field order) followed by one line of nested constructor
    calls, e.g. `Data(Positions([Mid("Ahri",0.03,0.09,0.51,1)]))`. Close to Python literal syntax
    but uses `null`/`true`/`false` (JS/JSON tokens) instead of `None`/`True`/`False` -- see
    _parse_opgg_class_notation. This appears to be this whole server's universal response
    format (both tools exercised here used it, with no structuredContent populated on either),
    so the parser is written generically rather than as a one-off for either tool.

What's still a guess: whether "NA"/"KR"/"EUW"-style short codes are the FULL set of valid
`region` values for lol_get_summoner_profile (only tested with "KR"), and whether
lol_list_lane_meta_champions's `position` values and default field selection are exhaustive/
optimal (only tested "mid" and the full 5-lane field set). Re-run
scripts/explore_opgg_mcp.py/opgg_mcp_demo.py if either tool's behavior seems off after this.
"""
from __future__ import annotations

import ast
import json
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger(__name__)

_DEFAULT_URL = "https://mcp-api.op.gg/mcp"


class OpggMcpToolError(Exception):
    """Raised when the server itself reports a tool call failed (CallToolResult.isError=True) --
    e.g. an unknown region code or a rejected/unrecognized argument. `payload` carries whatever
    the server sent back (usually explanatory text), surfaced as-is rather than swallowed, since
    that text is exactly what's needed to correct a wrong argument-name guess."""

    def __init__(self, tool_name: str, payload: Any):
        super().__init__(f"OP.GG MCP tool {tool_name!r} returned an error: {payload!r}")
        self.tool_name = tool_name
        self.payload = payload


@asynccontextmanager
async def opgg_session(url: str = _DEFAULT_URL):
    """Connects to the OP.GG MCP server and yields an initialized ClientSession. One connection
    per `async with` block -- callers doing several calls in a row (e.g. one summoner-profile
    lookup per roster player) should reuse a single session rather than reconnecting per call."""
    async with streamable_http_client(url) as (read_stream, write_stream, _get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


async def list_tool_schemas(session: ClientSession) -> list[dict]:
    """Ground truth: every tool the server currently exposes, with its real input/output JSON
    Schema. This is the discovery step scripts/explore_opgg_mcp.py runs -- use it before trusting
    any hand-written wrapper in this file (see module docstring's confidence-level notes)."""
    result = await session.list_tools()
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.inputSchema,
            "output_schema": tool.outputSchema,
        }
        for tool in result.tools
    ]


# ============================================================
# Response parsing -- the server's custom "class notation", not JSON. See module docstring.
# ============================================================

_CLASS_HEADER_RE = re.compile(r"^class\s+(\w+):\s*(.*)$")
_JS_LITERAL_RE = re.compile(r"\b(null|true|false)\b")
_JS_LITERAL_MAP = {"null": "None", "true": "True", "false": "False"}


def _parse_opgg_class_notation(text: str) -> Any:
    """Parses this server's `class Name: field1,field2` header + nested-constructor-call body
    into a plain nested dict/list structure. The body is close enough to Python call/list/literal
    syntax that ast.parse can do the heavy lifting once JS-style null/true/false are swapped for
    their Python equivalents -- this never calls eval()/exec(), only ast parsing and tree walking,
    so a malformed or even hostile response can't execute anything, at worst it raises."""
    lines = [line for line in text.splitlines() if line.strip()]
    field_map: dict[str, list[str]] = {}
    body_line = None
    for line in lines:
        m = _CLASS_HEADER_RE.match(line)
        if m:
            class_name, fields = m.groups()
            field_map[class_name] = [f.strip() for f in fields.split(",") if f.strip()]
        else:
            body_line = line  # the last non-header line is the data expression

    if body_line is None:
        raise ValueError("no data expression line found in OP.GG MCP class-notation response")

    normalized = _JS_LITERAL_RE.sub(lambda m: _JS_LITERAL_MAP[m.group(1)], body_line)
    tree = ast.parse(normalized, mode="eval").body
    return _resolve_class_notation_node(tree, field_map)


def _resolve_class_notation_node(node: ast.AST, field_map: dict[str, list[str]]) -> Any:
    if isinstance(node, ast.Call):
        class_name = node.func.id  # type: ignore[attr-defined]
        fields = field_map.get(class_name)
        if fields is None:
            raise ValueError(f"no header declaration for class {class_name!r}")
        if len(fields) != len(node.args):
            raise ValueError(
                f"class {class_name!r} declared {len(fields)} field(s) {fields} but call has "
                f"{len(node.args)} argument(s) -- response format may have changed"
            )
        return {
            field: _resolve_class_notation_node(arg, field_map)
            for field, arg in zip(fields, node.args)
        }
    if isinstance(node, ast.List):
        return [_resolve_class_notation_node(el, field_map) for el in node.elts]
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant):
        return -node.operand.value
    raise ValueError(f"unexpected node type in OP.GG MCP response: {type(node).__name__}")


def _parse_tool_result(result) -> Any:
    """CallToolResult carries its payload one of three ways in practice: structuredContent (a
    ready-to-use dict, if the server ever declares an output schema -- neither tool exercised
    here does), plain JSON text (in case some OTHER tool on this server uses it), or this
    server's custom class notation (confirmed live for both tools this file wraps). Falls back to
    the raw string if none of those parse -- a human-readable error message from the server is
    still useful to see, not just a parse failure."""
    if result.structuredContent is not None:
        return result.structuredContent
    for block in result.content:
        if getattr(block, "type", None) == "text":
            text = block.text
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
            try:
                return _parse_opgg_class_notation(text)
            except (SyntaxError, ValueError) as e:
                logger.warning("Could not parse OP.GG MCP response as JSON or class notation: %s", e)
                return text
    return None


async def call_tool(session: ClientSession, name: str, arguments: dict[str, Any]) -> Any:
    """Generic tool invocation -- calls `name` with `arguments`, raises OpggMcpToolError if the
    server reports failure, otherwise returns the parsed payload (see _parse_tool_result)."""
    logger.info("Calling OP.GG MCP tool %s with arguments=%r", name, arguments)
    result = await session.call_tool(name, arguments)
    if result.isError:
        raise OpggMcpToolError(name, _parse_tool_result(result))
    return _parse_tool_result(result)


# ============================================================
# Typed wrappers for the two target tools -- schemas confirmed live, see module docstring.
# ============================================================


@dataclass
class SummonerLeagueEntry:
    game_type: str | None
    tier: str | None
    division: int | None
    lp: int | None
    wins: int | None
    losses: int | None


@dataclass
class SummonerProfile:
    game_name: str | None
    tagline: str | None
    level: int | None
    league_entries: list[SummonerLeagueEntry]


_SUMMONER_PROFILE_FIELDS = [
    "data.summoner.{game_name,tagline,level}",
    "data.summoner.league_stats[].{game_type,win,lose}",
    "data.summoner.league_stats[].tier_info.{tier,division,lp}",
]


async def get_summoner_profile(
    session: ClientSession, *, game_name: str, tag_line: str, region: str,
    lang: str = "en_US", **extra_arguments: Any,
) -> SummonerProfile:
    """region: OP.GG's own short region codes (e.g. "NA", "KR", "EUW"), NOT Riot's platform ids
    (na1/kr/euw1) -- confirmed live for "KR" against a real Riot ID; other region codes are
    inferred from the input schema's own examples (KR, BR, EUNE) but not individually tested.
    Unranked queues (no games played, e.g. FLEXRANKED/ARENA for an inactive player) come back
    with every field null -- those still appear as an entry with all-None fields rather than
    being omitted, so filter on e.g. `entry.wins is not None` if you only want played queues."""
    arguments = {
        "game_name": game_name, "tag_line": tag_line, "region": region, "lang": lang,
        "desired_output_fields": _SUMMONER_PROFILE_FIELDS,
        **extra_arguments,
    }
    payload = await call_tool(session, "lol_get_summoner_profile", arguments)
    summoner = (payload or {}).get("data", {}).get("summoner", {}) or {}
    entries = [
        SummonerLeagueEntry(
            game_type=stat.get("game_type"),
            tier=(stat.get("tier_info") or {}).get("tier"),
            division=(stat.get("tier_info") or {}).get("division"),
            lp=(stat.get("tier_info") or {}).get("lp"),
            wins=stat.get("win"),
            losses=stat.get("lose"),
        )
        for stat in summoner.get("league_stats", []) or []
    ]
    return SummonerProfile(
        game_name=summoner.get("game_name"), tagline=summoner.get("tagline"),
        level=summoner.get("level"), league_entries=entries,
    )


@dataclass
class LaneMetaChampion:
    champion_name: str | None
    lane: str | None  # this project's TOP/JUNGLE/MID/BOTTOM/SUPPORT vocabulary, not OP.GG's own
    win_rate: float | None
    pick_rate: float | None
    ban_rate: float | None
    tier: int | None
    kda: float | None
    games_played: int | None
    rank: int | None  # this champion's rank WITHIN its lane (1 = most-picked/highest-tier slot)


# OP.GG's own lane keys -> this project's role vocabulary (matches ingest/tier_list.py's
# _OPGG_POSITION_TO_ROLE for the unofficial REST endpoint, which uses "ADC" where this tool uses
# lowercase "adc" -- same mapping target, different source casing).
_OPGG_LANE_TO_ROLE = {"top": "TOP", "jungle": "JUNGLE", "mid": "MID", "adc": "BOTTOM", "support": "SUPPORT"}

_LANE_META_FIELDS = [
    f"data.positions.{lane}[].{{champion,ban_rate,pick_rate,win_rate,tier,kda,play,rank}}"
    for lane in _OPGG_LANE_TO_ROLE
]


async def list_lane_meta_champions(
    session: ClientSession, *, position: str = "all", lang: str = "en_US", **extra_arguments: Any,
) -> list[LaneMetaChampion]:
    """position: one of "all"/"none"/"top"/"mid"/"jungle"/"adc"/"support" per the tool's own
    input schema (confirmed live) -- NOT a region or rank-tier filter, this tool takes neither.
    Requests all 5 lanes' fields regardless of `position` so one call returns everything; if the
    server only populates the lane(s) matching `position`, the rest simply come back as empty
    lists (harmless) -- confirmed live only for position="mid"."""
    arguments = {
        "position": position, "lang": lang,
        "desired_output_fields": _LANE_META_FIELDS,
        **extra_arguments,
    }
    payload = await call_tool(session, "lol_list_lane_meta_champions", arguments)
    positions = (payload or {}).get("data", {}).get("positions", {}) or {}
    rows = []
    for lane_key, entries in positions.items():
        role = _OPGG_LANE_TO_ROLE.get(lane_key, lane_key.upper())
        for entry in entries or []:
            rows.append(LaneMetaChampion(
                champion_name=entry.get("champion"),
                lane=role,
                win_rate=entry.get("win_rate"),
                pick_rate=entry.get("pick_rate"),
                ban_rate=entry.get("ban_rate"),
                tier=entry.get("tier"),
                kda=entry.get("kda"),
                games_played=entry.get("play"),
                rank=entry.get("rank"),
            ))
    return rows
