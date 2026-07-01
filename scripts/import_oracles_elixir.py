#!/usr/bin/env python3
"""Imports an Oracle's Elixir pro-match CSV into the database and recomputes the derived pro
synergy/matchup/role-eligibility aggregates.

Usage: python scripts/import_oracles_elixir.py path/to/oe_data.csv
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from draftassistant.db import connection  # noqa: E402
from draftassistant.ingest import oracles_elixir  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python scripts/import_oracles_elixir.py path/to/oe_data.csv")
        sys.exit(1)

    csv_path = Path(sys.argv[1])
    if not csv_path.exists():
        print(f"File not found: {csv_path}")
        sys.exit(1)

    conn = connection.get_conn()
    try:
        summary = oracles_elixir.import_csv(conn, csv_path)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"Processed {summary['rows_processed']} player rows ({summary['games_count']} games, "
          f"{summary['teams_count']} teams) from {csv_path}")
    print(f"Upserted {summary['rows_upserted']} rows into oe_games_raw")
    if summary["rows_unresolved"]:
        print(f"WARNING: {summary['rows_unresolved']} rows skipped -- unresolved champion names: "
              f"{summary['unresolved_champion_names']}")
        print("Add entries to OE_NAME_ALIASES in src/draftassistant/ingest/oracles_elixir.py if these "
              "are legitimate naming mismatches.")


if __name__ == "__main__":
    main()
