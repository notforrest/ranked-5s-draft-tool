#!/usr/bin/env python3
"""Add or update a roster member. The web UI's setup screen only lets you SELECT from
existing roster players (by design -- this is a single-driver tool, one person maintains
the roster) -- this script is how you actually populate it.

Usage:
  python scripts/add_player.py "Display Name" "RiotGameName" "TagLine" [--region na1] [--roles TOP,JUNGLE]

Example:
  python scripts/add_player.py "Alex" "AlexPlays" "NA1" --roles TOP
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from draftassistant.db import connection  # noqa: E402
from draftassistant.db.repositories import roster_repo  # noqa: E402

PLATFORM_TO_ACCOUNT_REGION = {
    "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas", "oc1": "americas",
    "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe",
    "kr": "asia", "jp1": "asia",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("display_name")
    parser.add_argument("riot_game_name", help="Riot ID before the #, e.g. 'AlexPlays'")
    parser.add_argument("tag_line", help="Riot ID after the #, e.g. 'NA1'")
    parser.add_argument("--platform-region", default="na1", help="e.g. na1, euw1, kr (default: na1)")
    parser.add_argument("--roles", default="", help="comma-separated preferred roles, e.g. TOP,JUNGLE")
    args = parser.parse_args()

    account_region = PLATFORM_TO_ACCOUNT_REGION.get(args.platform_region.lower())
    if account_region is None:
        print(f"Unknown platform region '{args.platform_region}'. Known: {sorted(PLATFORM_TO_ACCOUNT_REGION)}")
        sys.exit(1)

    roles = [r.strip().upper() for r in args.roles.split(",") if r.strip()]

    conn = connection.get_conn()
    player_id = roster_repo.upsert_player(
        conn,
        display_name=args.display_name,
        riot_game_name=args.riot_game_name,
        riot_tag_line=args.tag_line,
        platform_region=args.platform_region.lower(),
        account_region=account_region,
        preferred_roles=roles,
    )
    conn.commit()
    conn.close()
    print(f"Saved player_id={player_id}: {args.display_name} ({args.riot_game_name}#{args.tag_line})")


if __name__ == "__main__":
    main()
