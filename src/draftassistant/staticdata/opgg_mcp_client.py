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
from mcp.shared.exceptions import McpError

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
    """Generic tool invocation -- raises OpggMcpToolError on EITHER of the two distinct failure
    modes MCP allows (confirmed live, both real): a soft failure the tool itself reports
    (CallToolResult.isError=True, e.g. a malformed argument the tool validated itself), or a hard
    JSON-RPC-level error the server raises before a result even exists (mcp.shared.exceptions.
    McpError -- confirmed live via a real "Summoner not found" error for an invalid Riot ID,
    which surfaces this way, NOT as isError=True). Callers that only caught OpggMcpToolError
    before this fix would see an unhandled McpError instead for exactly this kind of failure --
    normalizing both into one exception type here means every caller only ever needs one except
    clause."""
    logger.info("Calling OP.GG MCP tool %s with arguments=%r", name, arguments)
    try:
        result = await session.call_tool(name, arguments)
    except McpError as e:
        raise OpggMcpToolError(name, str(e)) from e
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
class ChampionPoolEntry:
    champion_name: str | None
    games: int | None
    wins: int | None
    losses: int | None


@dataclass
class SummonerProfile:
    game_name: str | None
    tagline: str | None
    level: int | None
    league_entries: list[SummonerLeagueEntry]
    champion_pool: list[ChampionPoolEntry]


_SUMMONER_PROFILE_FIELDS = [
    "data.summoner.{game_name,tagline,level}",
    "data.summoner.league_stats[].{game_type,win,lose}",
    "data.summoner.league_stats[].tier_info.{tier,division,lp}",
    "data.summoner.ranked_most_champions.my_champion_stats[].{champion_name,play,win,lose}",
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
    being omitted, so filter on e.g. `entry.wins is not None` if you only want played queues.
    champion_pool is this SEASON's ranked_most_champions (confirmed live, top-10-ish by games
    played) -- distinct from and NOT a replacement for personal_champion_stats (this app's own
    Riot-Match-V5-sourced, per-role, exact-season-boundary data for ROSTER players specifically);
    this is most useful for a player NOT on the roster (scouting), where personal_champion_stats
    has nothing at all."""
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
    champion_pool = [
        ChampionPoolEntry(
            champion_name=stat.get("champion_name"), games=stat.get("play"),
            wins=stat.get("win"), losses=stat.get("lose"),
        )
        for stat in (summoner.get("ranked_most_champions") or {}).get("my_champion_stats", []) or []
    ]
    return SummonerProfile(
        game_name=summoner.get("game_name"), tagline=summoner.get("tagline"),
        level=summoner.get("level"), league_entries=entries, champion_pool=champion_pool,
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


def to_opgg_champion_key(display_name: str) -> str:
    """DDragon display name (e.g. "Vel'Koz", "Twisted Fate", "Wukong") -> the identifier
    get_champion_analysis/get_lane_matchup_guide expect. Confirmed live that this tool family is
    forgiving of format: "KAISA", "KAI_SA", and "KAI'SA" all worked identically, as did
    "WUKONG" and DDragon's own internal key "MONKEYKING" (both resolve to the same champion) --
    so a simple "strip everything but letters/digits, uppercase" transform is sufficient; no
    per-champion alias table needed."""
    return re.sub(r"[^A-Za-z0-9]", "", display_name).upper()


_ROLE_TO_OPGG_POSITION = {v: k for k, v in _OPGG_LANE_TO_ROLE.items()}  # e.g. "BOTTOM" -> "adc"


@dataclass
class ChampionCounter:
    champion_name: str | None
    win_rate: float | None  # this is the COUNTER champion's win rate, i.e. > 0.5 means it beats you
    games: int | None


@dataclass
class ChampionAllySynergy:
    ally_position: str | None  # this project's role vocabulary
    ally_champion_name: str | None
    win_rate: float | None
    games: int | None


@dataclass
class ChampionAnalysis:
    champion_name: str
    position: str  # this project's role vocabulary
    win_rate: float | None
    pick_rate: float | None
    ban_rate: float | None
    tier: int | None
    strong_counters: list[ChampionCounter]  # champions that beat THIS champion
    weak_counters: list[ChampionCounter]    # champions THIS champion beats
    synergies: list[ChampionAllySynergy]


_CHAMPION_ANALYSIS_FIELDS = [
    "data.summary.positions[].name",
    "data.summary.positions[].stats.{ban_rate,pick_rate,win_rate}",
    "data.summary.positions[].stats.tier_data.tier",
    "data.strong_counters[].{champion_name,win_rate,play}",
    "data.weak_counters[].{champion_name,win_rate,play}",
    *[f"data.synergies.{lane}[].{{synergy_champion_name,win_rate,play}}" for lane in _OPGG_LANE_TO_ROLE],
]


async def get_champion_analysis(
    session: ClientSession, *, champion: str, position: str, game_mode: str = "ranked",
    lang: str = "en_US", **extra_arguments: Any,
) -> ChampionAnalysis:
    """champion: a display name (e.g. "Vel'Koz") -- converted internally via
    to_opgg_champion_key. position MUST be one of top/mid/jungle/adc/support -- "all" is listed
    in the input schema's enum but the server rejects it live ("The selected position is
    invalid."); this is a real, confirmed server quirk, not a guess.

    IMPORTANT confirmed-live quirk: data.summary.positions[] always lists EVERY position the
    champion is played in (e.g. Vel'Koz returns SUPPORT, MID, AND ADC entries) regardless of the
    `position` argument, and is NOT ordered to put the requested position first (Vel'Koz with
    position="mid" returned SUPPORT at index 0). The requested position's stats are located by
    matching `name` explicitly, never by assuming positions[0].

    This is a per-champion-per-position call (~2s each, live-measured) -- there is no bulk
    "every champion" variant. See ingest/opgg_mcp_ingest.py for how this gets used for a bounded
    subset of champions (not all ~170 x 5 positions) rather than exhaustively."""
    arguments = {
        "game_mode": game_mode, "champion": to_opgg_champion_key(champion), "position": position,
        "lang": lang, "desired_output_fields": _CHAMPION_ANALYSIS_FIELDS,
        **extra_arguments,
    }
    payload = await call_tool(session, "lol_get_champion_analysis", arguments)
    data = (payload or {}).get("data", {}) or {}
    all_positions = (data.get("summary") or {}).get("positions") or []
    # OP.GG's own position names are TOP/JUNGLE/MID/ADC/SUPPORT -- match against `position.upper()`
    # directly here, NOT _OPGG_LANE_TO_ROLE (which maps "adc" to this project's "BOTTOM", a name
    # the API response itself never uses).
    matched = next((p for p in all_positions if p.get("name") == position.upper()), {})
    stats = matched.get("stats") or {}

    strong = [
        ChampionCounter(champion_name=c.get("champion_name"), win_rate=c.get("win_rate"), games=c.get("play"))
        for c in data.get("strong_counters", []) or []
    ]
    weak = [
        ChampionCounter(champion_name=c.get("champion_name"), win_rate=c.get("win_rate"), games=c.get("play"))
        for c in data.get("weak_counters", []) or []
    ]
    synergies = [
        ChampionAllySynergy(
            ally_position=_OPGG_LANE_TO_ROLE.get(lane_key, lane_key.upper()),
            ally_champion_name=s.get("synergy_champion_name"), win_rate=s.get("win_rate"), games=s.get("play"),
        )
        for lane_key, entries in (data.get("synergies") or {}).items()
        for s in entries or []
    ]
    return ChampionAnalysis(
        champion_name=champion, position=_OPGG_LANE_TO_ROLE.get(position, position.upper()),
        win_rate=stats.get("win_rate"), pick_rate=stats.get("pick_rate"), ban_rate=stats.get("ban_rate"),
        tier=(stats.get("tier_data") or {}).get("tier"),
        strong_counters=strong, weak_counters=weak, synergies=synergies,
    )


async def get_lane_matchup_guide(
    session: ClientSession, *, my_champion: str, opponent_champion: str, position: str,
    lang: str = "en_US", **extra_arguments: Any,
) -> dict:
    """Returns the raw parsed payload (not a typed dataclass) -- this tool's response is a large,
    qualitative "matchup guide" (runes/summoner-spells/counters context for a specific champion
    matchup), more suited to being displayed close to verbatim than reshaped into rigid fields.
    Intended for pre-draft prep (e.g. scripts/opgg_mcp_demo.py-style manual lookups), NOT the
    live hover panel -- see this module's docstring on why live network calls don't belong on
    that path. Same "all" position quirk as get_champion_analysis applies here."""
    arguments = {
        "position": position, "my_champion": to_opgg_champion_key(my_champion),
        "opponent_champion": to_opgg_champion_key(opponent_champion), "lang": lang,
        **extra_arguments,
    }
    return await call_tool(session, "lol_get_lane_matchup_guide", arguments)


@dataclass
class RecentMatch:
    game_type: str | None
    created_at: str | None
    game_length_second: int | None
    champion_name: str | None
    result: str | None  # e.g. "WIN"/"LOSE" -- passed through as-is, not normalized to bool
    kills: int | None
    deaths: int | None
    assists: int | None


_SUMMONER_MATCHES_FIELDS = [
    "data.game_history[].{created_at,game_length_second,game_type}",
    "data.game_history[].participants[].{champion_name,position}",
    "data.game_history[].participants[].stats.{assist,death,kill,result}",
]


async def list_summoner_matches(
    session: ClientSession, *, game_name: str, tag_line: str, region: str, limit: int = 10,
    lang: str = "en_US", **extra_arguments: Any,
) -> list[RecentMatch]:
    """limit is clamped server-side to [5, 20] per the input schema. Confirmed live: each
    match's participants[] contains exactly ONE entry (the queried summoner) -- the tool's own
    description ("target summoner only, excludes enemy stats") is accurate, not aspirational.
    NOT wired into the roster refresh pipeline (personal_champion_stats already covers roster
    players far more precisely from Riot's own Match-V5 data) -- this exists for looking up
    players who AREN'T on the roster (opponent scouting), where nothing else in this codebase
    can reach at all."""
    arguments = {
        "game_name": game_name, "tag_line": tag_line, "region": region, "limit": limit,
        "lang": lang, "desired_output_fields": _SUMMONER_MATCHES_FIELDS,
        **extra_arguments,
    }
    payload = await call_tool(session, "lol_list_summoner_matches", arguments)
    games = (payload or {}).get("data", {}).get("game_history", []) or []
    matches = []
    for game in games:
        participants = game.get("participants") or []
        me = participants[0] if participants else {}
        stats = me.get("stats") or {}
        matches.append(RecentMatch(
            game_type=game.get("game_type"), created_at=game.get("created_at"),
            game_length_second=game.get("game_length_second"), champion_name=me.get("champion_name"),
            result=stats.get("result"), kills=stats.get("kill"), deaths=stats.get("death"),
            assists=stats.get("assist"),
        ))
    return matches
