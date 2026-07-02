#!/usr/bin/env python3
"""Applies schema.sql to a fresh data/lol_draft.db. Safe to re-run against a missing DB file;
refuses to run against an existing one to avoid silently wiping data (delete the file yourself
first if you really want a clean slate)."""
from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from draftassistant import config  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")


def main() -> None:
    print("Ensuring data directories exist...")
    config.ensure_data_dirs()
    schema_path = Path(__file__).resolve().parents[1] / "src" / "draftassistant" / "db" / "schema.sql"

    if config.DB_PATH.exists():
        print(f"Refusing to overwrite existing database at {config.DB_PATH}")
        print("Delete it yourself first if you want to start fresh.")
        sys.exit(1)

    print(f"Reading schema from {schema_path}...")
    schema_sql = schema_path.read_text()
    print(f"Creating database at {config.DB_PATH} and applying schema...")
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.executescript(schema_sql)
        conn.commit()
    finally:
        conn.close()

    print(f"Initialized database at {config.DB_PATH}")


if __name__ == "__main__":
    main()
