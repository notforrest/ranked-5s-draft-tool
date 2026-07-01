#!/usr/bin/env python3
"""Lists roster players (with their internal player_id), or permanently removes one along with
all of their match/mastery/session history -- for cleaning up a duplicate or mistaken roster
entry (e.g. a stale row left over from before a Riot ID correction, or one created in error).

Usage:
  python scripts/remove_player.py --list
  python scripts/remove_player.py --player-id 7
  python scripts/remove_player.py --riot-id "OldGameName#NA1"

After removing a duplicate, re-run scripts/import_roster.py to (re-)add the correct entry from
your roster.yaml -- this delete only clears the stale row, it doesn't re-import anything.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from draftassistant.db import connection  # noqa: E402
from draftassistant.db.repositories import roster_repo  # noqa: E402


def _print_roster(conn) -> None:
    players = roster_repo.get_active_players(conn)
    if not players:
        print("No players in the roster.")
        return
    for p in players:
        print(f"player_id={p['player_id']:<4} {p['display_name']:<15} "
              f"{p['riot_game_name']}#{p['riot_tag_line']:<10} puuid={p['puuid'] or '(none)'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="list current roster players with their IDs")
    group.add_argument("--player-id", type=int, help="internal player_id to remove (see --list)")
    group.add_argument("--riot-id", help='Riot ID to remove, e.g. "GameName#TagLine"')
    args = parser.parse_args()

    conn = connection.get_conn()
    try:
        if args.list:
            _print_roster(conn)
            return

        if args.riot_id:
            if "#" not in args.riot_id:
                print('--riot-id must be in "GameName#TagLine" form')
                sys.exit(1)
            game_name, tag_line = args.riot_id.split("#", 1)
            player = next(
                (p for p in roster_repo.get_active_players(conn)
                 if p["riot_game_name"] == game_name and p["riot_tag_line"] == tag_line),
                None,
            )
            if player is None:
                print(f"No active player found with Riot ID {args.riot_id}")
                sys.exit(1)
            player_id = player["player_id"]
        else:
            player_id = args.player_id

        player = roster_repo.get_player(conn, player_id)
        if player is None:
            print(f"No player found with player_id={player_id}")
            sys.exit(1)

        counts = roster_repo.delete_player(conn, player_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"Removed player_id={player_id} ({player['display_name']}, "
          f"{player['riot_game_name']}#{player['riot_tag_line']}).")
    extra = {t: c for t, c in counts.items() if t != "players" and c}
    if extra:
        print("Also removed dependent rows:")
        for table, count in extra.items():
            print(f"  {table}: {count}")


if __name__ == "__main__":
    main()
