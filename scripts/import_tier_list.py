#!/usr/bin/env python3
"""Imports a curated global tier-list YAML file (or every *.yaml file in a directory) into the
database.

Usage:
  python scripts/import_tier_list.py data/curated/tier_list/14.13.yaml
  python scripts/import_tier_list.py data/curated/tier_list/
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from draftassistant.db import connection  # noqa: E402
from draftassistant.ingest import tier_list  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python scripts/import_tier_list.py path/to/file.yaml (or a directory)")
        sys.exit(1)

    target = Path(sys.argv[1])
    if not target.exists():
        print(f"Path not found: {target}")
        sys.exit(1)

    if target.is_dir():
        # Note: *.yaml only -- the EXAMPLE.yaml.template file is deliberately named with a
        # .template suffix precisely so a directory import doesn't pick it up by accident.
        yaml_files = sorted(target.glob("*.yaml"))
    else:
        yaml_files = [target]

    if not yaml_files:
        print(f"No *.yaml files found in {target}")
        sys.exit(1)

    conn = connection.get_conn()
    try:
        for yaml_path in yaml_files:
            summary = tier_list.import_tier_list_file(conn, yaml_path)
            print(f"{yaml_path.name} (patch {summary['patch']}): imported "
                  f"{summary['entries_imported']}/{summary['entries_processed']} entries")
            if summary["entries_unresolved"]:
                print(f"  WARNING: {summary['entries_unresolved']} unresolved champion names: "
                      f"{summary['unresolved_champion_names']}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
