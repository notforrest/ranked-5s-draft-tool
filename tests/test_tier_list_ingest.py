"""Tests for the automated OP.GG tier-list import (ingest/tier_list.py:import_from_opgg)."""
from __future__ import annotations

import httpx
import respx

from draftassistant.db.repositories import champion_repo, tierlist_repo
from draftassistant.ingest import tier_list


def _seed_champion(conn, champion_id: int, name: str) -> None:
    champion_repo.upsert_champion(
        conn, champion_id=champion_id, champion_key=name, name=name, tags=["Mage"],
        icon_path=f"https://example.com/{name}.png", ddragon_version="14.24.1",
    )


def _opgg_payload() -> dict:
    return {
        "meta": {"version": "14.24.1", "match_count": 1_000_000},
        "data": [
            {
                # Ahri: has per-position data -- exercise the precise path.
                "id": 103,
                "average_stats": {"win_rate": 0.512, "pick_rate": 0.08, "kda": 3.1, "tier": 2, "rank": 10},
                "roles": [],
                "positions": [
                    {"name": "MID", "stats": {"win": 5000, "play": 10000, "win_rate": 0.50, "role_rate": 0.9},
                     "roles": [], "counters": []},
                ],
            },
            {
                # Zed: no positions data at all -- exercise the role_eligibility fallback path.
                "id": 238,
                "average_stats": {"win_rate": 0.48, "pick_rate": 0.11, "kda": 2.5, "tier": 3, "rank": 40},
                "roles": [],
                "positions": [],
            },
            {
                # Unknown to our champions table entirely -- should be skipped and counted.
                "id": 999999,
                "average_stats": {"win_rate": 0.5, "pick_rate": 0.01},
                "roles": [],
                "positions": [],
            },
            {
                # Known champion, no positions AND no role_eligibility yet -- nothing to file it
                # under, should be skipped rather than guessed at.
                "id": 67,
                "average_stats": {"win_rate": 0.5, "pick_rate": 0.10},
                "roles": [],
                "positions": [],
            },
        ],
    }


def test_import_from_opgg_precise_and_fallback_paths(test_db_conn):
    _seed_champion(test_db_conn, 103, "Ahri")
    _seed_champion(test_db_conn, 238, "Zed")
    _seed_champion(test_db_conn, 67, "Vayne")  # no role_eligibility seeded -- should be skipped
    champion_repo.replace_role_eligibility(test_db_conn, "pro", [(238, "MID", 500)])
    test_db_conn.commit()

    with respx.mock:
        respx.get("https://lol-api-champion.op.gg/api/global/champions/ranked").mock(
            return_value=httpx.Response(200, json=_opgg_payload())
        )
        summary = tier_list.import_from_opgg(test_db_conn)

    assert summary["patch"] == "14.24"  # normalized from "14.24.1"
    assert summary["entries_imported"] == 2  # Ahri/MID + Zed/MID
    assert summary["champions_skipped_unresolved"] == [999999]
    assert summary["champions_skipped_no_known_role"] == [67]

    ahri_entry = tierlist_repo.get_tier_entry(test_db_conn, 103, "MID", "14.24")
    assert ahri_entry is not None
    assert ahri_entry["win_rate"] == 0.50  # precise per-position figure, not average_stats' 0.512
    assert ahri_entry["pick_rate"] == 0.08  # always average_stats, documented simplification
    assert ahri_entry["sample_size"] == 10000
    assert "op.gg" in ahri_entry["source_note"]

    zed_entry = tierlist_repo.get_tier_entry(test_db_conn, 238, "MID", "14.24")
    assert zed_entry is not None
    assert zed_entry["win_rate"] == 0.48  # fallback: average_stats, no positions data available
    assert zed_entry["pick_rate"] == 0.11
    assert zed_entry["sample_size"] is None

    # Vayne (67) has no row at all -- correctly skipped, not guessed at.
    assert tierlist_repo.get_tier_entry(test_db_conn, 67, "TOP", "14.24") is None


def test_import_from_opgg_is_reachable_only_from_the_setup_side(test_db_conn):
    """Documentation-as-test: the automated fetch must never be importable from the live-draft
    request path (api/routers/draft.py), same architectural rule as the Riot refresh job."""
    import pathlib
    draft_router = (pathlib.Path(__file__).resolve().parents[1]
                    / "src" / "draftassistant" / "api" / "routers" / "draft.py")
    assert "opgg_client" not in draft_router.read_text()
    assert "import_from_opgg" not in draft_router.read_text()
