#!/usr/bin/env python3
"""Fetches the current champion list from Data Dragon (no API key needed) and upserts it into
the champions table. Safe to re-run -- one-time initially, then again after major patches."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from draftassistant.db import connection  # noqa: E402
from draftassistant.staticdata import ddragon_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")


def main() -> None:
    print("Connecting to database...")
    conn = connection.get_conn()
    try:
        print("Fetching latest patch version from Data Dragon...")
        version = ddragon_client.get_latest_version()
        print(f"Latest patch: {version}. Syncing champion list...")
        ddragon_client.sync_champions(conn)
        conn.commit()
    finally:
        conn.close()
    print(f"Synced champions for patch {version}")


if __name__ == "__main__":
    main()
