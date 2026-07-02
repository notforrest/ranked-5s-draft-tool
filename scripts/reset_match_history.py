#!/usr/bin/env python3
"""Resets a player's (or every active player's) refresh watermark so their NEXT "Refresh Data"
run re-lists their full ranked match history via the new paginated backfill (see
riot_client/endpoints.py:get_all_ranked_match_ids), instead of only fetching matches newer than
whatever the old, queue-agnostic 50-match-window backfill had already advanced past.

Needed one-time after upgrading to the paginated backfill: any player refreshed under the old
code already has a watermark set, so without this reset a future refresh would only look
forward from that point, never going back to pick up ranked games the old narrow window missed
(the "Draven says 2 games in the tool, 17 on OP.GG" bug). This does NOT delete any already-cached
match data -- it only clears the watermark; matches already on disk/in the DB are skipped via
match_exists() during the re-backfill, so re-running refresh afterward is cheap.

Usage:
  python scripts/reset_match_history.py --list
  python scripts/reset_match_history.py --player-id 7
  python scripts/reset_match_history.py --riot-id "edd#fps"
  python scripts/reset_match_history.py --all

After resetting, run scripts/run_pre_draft_refresh.py (or the setup screen's "Refresh Data"
button) to actually perform the full backfill.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from draftassistant.db import connection  # noqa: E402
from draftassistant.db.repositories import match_repo, roster_repo  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")


def _print_roster(conn) -> None:
    players = roster_repo.get_active_players(conn)
    if not players:
        print("No players in the roster.")
        return
    for p in players:
        state = match_repo.get_refresh_state(conn, p["player_id"])
        watermark = state["last_match_fetched_ms"] if state else None
        print(f"player_id={p['player_id']:<4} {p['display_name']:<15} "
              f"{p['riot_game_name']}#{p['riot_tag_line']:<10} "
              f"watermark={'(none)' if watermark is None else watermark}")


def _resolve_player_id(conn, riot_id: str) -> int:
    if "#" not in riot_id:
        print('--riot-id must be in "GameName#TagLine" form')
        sys.exit(1)
    game_name, tag_line = riot_id.split("#", 1)
    player = next(
        (p for p in roster_repo.get_active_players(conn)
         if p["riot_game_name"] == game_name and p["riot_tag_line"] == tag_line),
        None,
    )
    if player is None:
        print(f"No active player found with Riot ID {riot_id}")
        sys.exit(1)
    return player["player_id"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="list roster players and their current watermark")
    group.add_argument("--player-id", type=int, help="internal player_id to reset (see --list)")
    group.add_argument("--riot-id", help='Riot ID to reset, e.g. "edd#fps"')
    group.add_argument("--all", action="store_true", help="reset every active roster player")
    args = parser.parse_args()

    print("Connecting to database...")
    conn = connection.get_conn()
    try:
        if args.list:
            _print_roster(conn)
            return

        if args.all:
            targets = roster_repo.get_active_players(conn)
            print(f"Resetting watermark for all {len(targets)} active player(s)...")
        else:
            player_id = args.player_id if args.player_id is not None else _resolve_player_id(conn, args.riot_id)
            player = roster_repo.get_player(conn, player_id)
            if player is None:
                print(f"No player found with player_id={player_id}")
                sys.exit(1)
            targets = [player]

        reset_count = 0
        for i, player in enumerate(targets):
            had_watermark = match_repo.reset_refresh_state(conn, player["player_id"])
            if had_watermark:
                reset_count += 1
            print(f"[{i + 1}/{len(targets)}] {player['display_name']} "
                  f"({player['riot_game_name']}#{player['riot_tag_line']}): "
                  f"{'watermark cleared' if had_watermark else 'no watermark to clear'}.")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"\n{reset_count} player(s) had a watermark cleared. Run scripts/run_pre_draft_refresh.py "
          f"(or the setup screen's Refresh Data button) to backfill their full ranked history.")


if __name__ == "__main__":
    main()
