#!/usr/bin/env python3
"""Applies any tables/indexes in schema.sql that don't exist yet in the real database --
without touching (or requiring you to delete) an existing DB, unlike init_db.py, which refuses
to run against one at all.

This project has no real migration framework (schema_migrations exists in schema.sql but is
never written to or read anywhere) -- schema.sql is meant to be the single source of truth, and
this script is the bridge for "I pulled a branch that added new tables to it, and my existing
database doesn't have them yet." It rewrites every `CREATE TABLE`/`CREATE INDEX`/
`CREATE UNIQUE INDEX` statement in schema.sql to include `IF NOT EXISTS` and executes the whole
thing as one script -- already-existing tables/indexes are silently skipped (their existing data
is completely untouched), only genuinely missing ones get created.

LIMITATION: this only adds missing tables/indexes. It does NOT add missing COLUMNS to a table
that already exists (e.g. if a future change adds a column to `players`), since that needs an
ALTER TABLE with real thought about backfilling existing rows, not a blind IF-NOT-EXISTS rewrite.
Column-level schema drift still needs a hand-written one-off migration when it happens.

Usage:
  python scripts/migrate_db.py
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from draftassistant import config  # noqa: E402

_CREATE_STATEMENT_RE = re.compile(
    r"\bCREATE\s+(TABLE|(?:UNIQUE\s+)?INDEX)\s+(?!IF NOT EXISTS)", re.IGNORECASE,
)


def _make_idempotent(schema_sql: str) -> str:
    return _CREATE_STATEMENT_RE.sub(lambda m: f"CREATE {m.group(1)} IF NOT EXISTS ", schema_sql)


def main() -> None:
    schema_path = Path(__file__).resolve().parents[1] / "src" / "draftassistant" / "db" / "schema.sql"

    if not config.DB_PATH.exists():
        print(f"No database found at {config.DB_PATH} -- run scripts/init_db.py instead to create one fresh.")
        sys.exit(1)

    before = set(_existing_tables(config.DB_PATH))

    print(f"Reading schema from {schema_path}...")
    idempotent_sql = _make_idempotent(schema_path.read_text())

    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.executescript(idempotent_sql)
        conn.commit()
    finally:
        after = set(_existing_tables(config.DB_PATH))
        conn.close()

    new_tables = sorted(after - before)
    if new_tables:
        print(f"Added {len(new_tables)} new table(s): {', '.join(new_tables)}")
    else:
        print("No new tables to add -- database already matches schema.sql's table list.")


def _existing_tables(db_path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


if __name__ == "__main__":
    main()
