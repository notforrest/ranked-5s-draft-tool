#!/usr/bin/env python3
"""Imports a roster YAML file into the database. Re-run after editing the file to add players
or update their info -- see data/curated/roster.yaml.template for the format.

Usage:
  python scripts/import_roster.py data/curated/roster.yaml
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from draftassistant.db import connection  # noqa: E402
from draftassistant.ingest import roster  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python scripts/import_roster.py path/to/roster.yaml")
        sys.exit(1)

    yaml_path = Path(sys.argv[1])
    if not yaml_path.exists():
        print(f"Path not found: {yaml_path}")
        sys.exit(1)

    conn = connection.get_conn()
    try:
        summary = roster.import_roster_file(conn, yaml_path)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"{yaml_path.name}: imported {summary['entries_imported']}/{summary['entries_processed']} players")
    if summary["entries_skipped"]:
        print(f"  WARNING: {summary['entries_skipped']} entries skipped: {summary['skipped_entries']}")


if __name__ == "__main__":
    main()
