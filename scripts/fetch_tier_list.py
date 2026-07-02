#!/usr/bin/env python3
"""Automatically fetches global champion win/pick rate from OP.GG's unofficial API and imports
it into global_tier_list -- no manual copying required. This is a best-effort, unofficial data
source (see src/draftassistant/staticdata/opgg_client.py's docstring) that can break if OP.GG
changes their API; the hand-maintained data/curated/tier_list/*.yaml + import_tier_list.py path
still works independently as a fallback.

Usage:
  python scripts/fetch_tier_list.py
  python scripts/fetch_tier_list.py --mode aram
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from draftassistant.db import connection  # noqa: E402
from draftassistant.ingest import tier_list  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", default="ranked", choices=["ranked", "aram"])
    args = parser.parse_args()

    print("Connecting to database...")
    conn = connection.get_conn()
    try:
        summary = tier_list.import_from_opgg(conn, mode=args.mode)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"Fetched patch {summary['patch']} ({summary['mode']}): imported "
          f"{summary['entries_imported']} role-entries across {summary['champions_processed']} champions")
    if summary["champions_skipped_unresolved"]:
        print(f"  {len(summary['champions_skipped_unresolved'])} champion IDs not found locally "
              f"-- run scripts/sync_champions.py first: {summary['champions_skipped_unresolved']}")
    if summary["champions_skipped_no_known_role"]:
        print(f"  {len(summary['champions_skipped_no_known_role'])} champions skipped (no known "
              f"role yet -- needs personal/pro match data first): {summary['champions_skipped_no_known_role']}")


if __name__ == "__main__":
    main()
