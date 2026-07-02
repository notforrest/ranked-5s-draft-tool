#!/usr/bin/env python3
"""Re-runs personal_champion_stats + role-eligibility + roster synergy/matchup aggregation from
whatever match data is ALREADY cached in the DB -- no Riot API call, no network needed.

Useful any time the aggregation logic itself changes (e.g. the season-boundary filter added
2026-07-01) and you want existing cached matches re-aggregated under the new rule immediately,
without waiting for -- or needing -- a fresh "Refresh Data" run.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from draftassistant.aggregate.personal_stats import recompute_personal_stats  # noqa: E402
from draftassistant.aggregate.roster_synergy import recompute_roster_synergy  # noqa: E402
from draftassistant.db import connection  # noqa: E402


def main() -> None:
    conn = connection.get_conn()
    try:
        recompute_personal_stats(conn)
        recompute_roster_synergy(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print("Recomputed personal_champion_stats, champion_role_eligibility(source='personal'), "
          "synergy_roster, and matchup_roster from cached match data.")


if __name__ == "__main__":
    main()
