#!/usr/bin/env python3
"""Connects to OP.GG's MCP server (https://mcp-api.op.gg/mcp) and prints every tool it exposes,
full input/output JSON Schema included -- ground truth, straight from the server's own
tools/list response.

RUN THIS FIRST, before trusting anything in staticdata/opgg_mcp_client.py's two typed wrappers
(get_summoner_profile, list_lane_meta_champions) -- their argument names/values are best-effort
guesses (see that module's docstring for exactly what's confirmed vs guessed) made without ever
being able to reach mcp-api.op.gg from the dev machine this was built on (every op.gg domain is
blocked there, same as Riot's own API). This machine is assumed unrestricted, so this is the
first real test of whether the prototype's assumptions about the protocol/tool names hold up.

Usage:
  python scripts/explore_opgg_mcp.py                    # list every tool
  python scripts/explore_opgg_mcp.py lol_get_summoner_profile lol_list_lane_meta_champions
                                                          # only show these tools (by name)
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from draftassistant.staticdata.opgg_mcp_client import list_tool_schemas, opgg_session  # noqa: E402


async def main(name_filter: set[str]) -> None:
    print("Connecting to https://mcp-api.op.gg/mcp ...")
    async with opgg_session() as session:
        tools = await list_tool_schemas(session)
    print(f"Server exposes {len(tools)} tool(s) total.\n")

    shown = 0
    for tool in tools:
        if name_filter and tool["name"] not in name_filter:
            continue
        shown += 1
        print(f"=== {tool['name']} ===")
        print(tool["description"] or "(no description)")
        print("\n-- input_schema --")
        print(json.dumps(tool["input_schema"], indent=2))
        if tool["output_schema"] is not None:
            print("\n-- output_schema --")
            print(json.dumps(tool["output_schema"], indent=2))
        print()

    if name_filter and shown == 0:
        print(f"None of the requested tool names {sorted(name_filter)} were found. "
              f"Full list of available names:")
        for tool in tools:
            print(f"  - {tool['name']}")


if __name__ == "__main__":
    asyncio.run(main(set(sys.argv[1:])))
