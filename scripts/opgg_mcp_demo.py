#!/usr/bin/env python3
"""Demo/smoke-test for the two OP.GG MCP tools this prototype targets.

Prints a summoner's rank/LP, and a sample of the current lane-meta tier list with ban_rate
(the whole reason this prototype exists -- see the ban-rate removal from the hover panel earlier
this session). Both tools' schemas were confirmed live 2026-07-01, see opgg_mcp_client.py's
module docstring for exactly what was and wasn't tested.

Usage:
  python scripts/opgg_mcp_demo.py "Hide on bush#KR1" --region KR
  python scripts/opgg_mcp_demo.py "SomeSummoner#NA1" --region NA --position top
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from draftassistant.staticdata.opgg_mcp_client import (  # noqa: E402
    OpggMcpToolError,
    get_summoner_profile,
    list_lane_meta_champions,
    opgg_session,
)


async def main(riot_id: str, region: str, position: str) -> None:
    if "#" not in riot_id:
        print('Riot ID must be in "GameName#TagLine" form')
        sys.exit(1)
    game_name, tag_line = riot_id.split("#", 1)

    async with opgg_session() as session:
        print(f"--- lol_get_summoner_profile({game_name!r}, {tag_line!r}, region={region!r}) ---")
        try:
            profile = await get_summoner_profile(
                session, game_name=game_name, tag_line=tag_line, region=region,
            )
            print(f"  game_name={profile.game_name!r} tagline={profile.tagline!r} level={profile.level}")
            played_entries = [e for e in profile.league_entries if e.wins is not None]
            if not played_entries:
                print("  (no queues with games played)")
            for entry in played_entries:
                print(f"  {entry.game_type}: {entry.tier} {entry.division} {entry.lp} LP "
                      f"({entry.wins}W {entry.losses}L)")
        except OpggMcpToolError as e:
            print(f"  FAILED: {e}")
            print("  -> check the error text above against scripts/explore_opgg_mcp.py's schema.")

        print(f"\n--- lol_list_lane_meta_champions(position={position!r}) ---")
        try:
            rows = await list_lane_meta_champions(session, position=position)
            print(f"  {len(rows)} row(s) returned")
            has_ban_rate = any(r.ban_rate is not None for r in rows)
            print(f"  ban_rate populated on at least one row: {has_ban_rate}")
            for row in rows[:5]:
                print(f"  {asdict(row)}")
        except OpggMcpToolError as e:
            print(f"  FAILED: {e}")
            print("  -> check the error text above against scripts/explore_opgg_mcp.py's schema.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("riot_id", help='"GameName#TagLine"')
    parser.add_argument("--region", default="NA", help="OP.GG region code, e.g. NA/KR/EUW")
    parser.add_argument("--position", default="all", help="all/none/top/mid/jungle/adc/support")
    args = parser.parse_args()
    asyncio.run(main(args.riot_id, args.region, args.position))
