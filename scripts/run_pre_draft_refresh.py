#!/usr/bin/env python3
"""Runs the pre-draft refresh job against the real database and the real Riot API key configured
in .env. This is the ONLY script that ever calls the Riot API -- run it before queuing up, never
during a live draft."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from draftassistant.db import connection  # noqa: E402
from draftassistant.refresh import pre_draft_refresh  # noqa: E402


def main() -> None:
    conn = connection.get_conn()
    try:
        summary = pre_draft_refresh.run(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"Refresh run #{summary['run_id']}: {summary['status']}")
    for p in summary["players"]:
        line = f"  - {p['display_name']}: {p['status']}"
        if p["status"] == "ok":
            line += f" ({p.get('new_matches_fetched', 0)} new matches, {p.get('mastery_champions', 0)} champs with mastery)"
        else:
            line += f" -- {p.get('error', 'unknown error')}"
        print(line)

    if summary.get("fatal_auth_error"):
        print(f"\nFATAL: {summary['fatal_auth_error']}")
        print("Your Riot API key is likely expired or invalid -- paste a fresh one into .env and retry.")

    if summary["status"] != "success":
        sys.exit(1)


if __name__ == "__main__":
    main()
