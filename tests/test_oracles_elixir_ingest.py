"""Tests for Oracle's Elixir CSV ingestion against the fixture CSV (2 small complete games)."""
from __future__ import annotations

from pathlib import Path

from draftassistant.db.repositories import champion_repo, oe_repo, synergy_repo
from draftassistant.ingest import oracles_elixir

FIXTURE_CSV = Path(__file__).resolve().parent / "fixtures" / "sample_oe_rows.csv"

# champion_key doesn't matter for this test's purposes; use the display name for both.
_FIXTURE_CHAMPIONS = [
    (1, "Malphite"), (2, "LeeSin", "Lee Sin"), (3, "Ahri"), (4, "Jinx"), (5, "Thresh"),
    (6, "Camille"), (7, "Vi"), (8, "Zed"), (9, "Vayne"), (10, "Nautilus"),
]


def _seed_champions(conn):
    for entry in _FIXTURE_CHAMPIONS:
        if len(entry) == 3:
            champ_id, key, name = entry
        else:
            champ_id, name = entry
            key = name
        champion_repo.upsert_champion(
            conn, champion_id=champ_id, champion_key=key, name=name, tags=["Fighter"],
            icon_path=f"https://example.com/{key}.png", ddragon_version="14.13.1",
        )


def test_import_csv_populates_oe_games_raw(test_db_conn):
    _seed_champions(test_db_conn)

    summary = oracles_elixir.import_csv(test_db_conn, FIXTURE_CSV)

    # 10 player rows per game x 2 games = 20 (team summary rows filtered out).
    assert summary["rows_processed"] == 20
    assert summary["rows_upserted"] == 20
    assert summary["rows_unresolved"] == 0
    assert summary["games_count"] == 2
    assert summary["teams_count"] == 4

    rows = test_db_conn.execute("SELECT * FROM oe_games_raw").fetchall()
    assert len(rows) == 20
    # Team-summary rows (position == 'team') must be excluded entirely.
    assert all(r["position"] != "team" for r in rows)
    # Every row should have resolved a champion_id.
    assert all(r["champion_id"] is not None for r in rows)


def test_import_csv_populates_synergy_pro(test_db_conn):
    _seed_champions(test_db_conn)
    oracles_elixir.import_csv(test_db_conn, FIXTURE_CSV)

    malphite = champion_repo.get_champion_by_name(test_db_conn, "Malphite")["champion_id"]
    lee_sin = champion_repo.get_champion_by_name(test_db_conn, "Lee Sin")["champion_id"]

    # Malphite + Lee Sin were teammates in both GAME1 (Team Alpha, win) and GAME2 (Team Gamma, loss).
    synergy = synergy_repo.get_synergy(test_db_conn, "synergy_pro", malphite, lee_sin)
    assert synergy is not None
    assert synergy["games_together"] == 2
    assert synergy["wins_together"] == 1  # won GAME1, lost GAME2

    # Sanity: every same-team pair within a 5-player team should be tallied --
    # C(5,2) = 10 pairs per team, 4 teams => 40 pair-occurrences, but many pairs overlap across
    # the two games' rosters (Malphite/LeeSin/Ahri/Jinx/Thresh appear on both winning-side
    # teams, and Camille/Vi/Zed/Vayne/Nautilus on both losing-side teams), so distinct pairs < 40.
    all_synergy_rows = test_db_conn.execute("SELECT * FROM synergy_pro").fetchall()
    assert len(all_synergy_rows) > 0
    total_games_together = sum(r["games_together"] for r in all_synergy_rows)
    # Each team contributes C(5,2)=10 pair-occurrences; 4 teams => 40 total pair-occurrences.
    assert total_games_together == 40


def test_import_csv_populates_matchup_pro_directionally(test_db_conn):
    _seed_champions(test_db_conn)
    oracles_elixir.import_csv(test_db_conn, FIXTURE_CSV)

    malphite = champion_repo.get_champion_by_name(test_db_conn, "Malphite")["champion_id"]
    camille = champion_repo.get_champion_by_name(test_db_conn, "Camille")["champion_id"]

    # Malphite (Team Alpha/Gamma, top) faced Camille (Team Beta/Delta, top) in both games.
    # GAME1: Malphite's team (Alpha) won. GAME2: Malphite's team (Gamma) lost.
    malphite_vs_camille = synergy_repo.get_matchup(test_db_conn, "matchup_pro", malphite, camille)
    assert malphite_vs_camille is not None
    assert malphite_vs_camille["games"] == 2
    assert malphite_vs_camille["wins_a"] == 1

    # Directional: Camille's record against Malphite must be tracked separately/correctly --
    # Camille's team lost GAME1, won GAME2, so wins_a here should be 1 as well (not blindly
    # mirrored from the other direction, but independently correct in this symmetric fixture).
    camille_vs_malphite = synergy_repo.get_matchup(test_db_conn, "matchup_pro", camille, malphite)
    assert camille_vs_malphite is not None
    assert camille_vs_malphite["games"] == 2
    assert camille_vs_malphite["wins_a"] == 1


def test_import_csv_populates_role_eligibility(test_db_conn):
    _seed_champions(test_db_conn)
    oracles_elixir.import_csv(test_db_conn, FIXTURE_CSV)

    malphite = champion_repo.get_champion_by_name(test_db_conn, "Malphite")["champion_id"]
    eligibility = champion_repo.get_role_eligibility(test_db_conn, malphite)
    assert "TOP" in eligibility
    assert eligibility["TOP"]["source"] == "pro"
    assert eligibility["TOP"]["games_observed"] == 2  # played top in both games

    jinx = champion_repo.get_champion_by_name(test_db_conn, "Jinx")["champion_id"]
    jinx_eligibility = champion_repo.get_role_eligibility(test_db_conn, jinx)
    assert "BOTTOM" in jinx_eligibility


def test_import_csv_skips_unresolved_champion_and_reports_count(test_db_conn):
    """Without seeding champions, every champion name should fail to resolve -- import must not
    crash, and the summary should report the unresolved count/names."""
    summary = oracles_elixir.import_csv(test_db_conn, FIXTURE_CSV)

    assert summary["rows_unresolved"] == 20
    assert summary["rows_upserted"] == 0
    assert "Malphite" in summary["unresolved_champion_names"]

    rows = test_db_conn.execute("SELECT * FROM oe_games_raw").fetchall()
    assert len(rows) == 0


def test_alias_map_resolves_wukong_to_monkeyking(test_db_conn):
    """The OE_NAME_ALIASES map should resolve a known historical name mismatch."""
    champion_repo.upsert_champion(
        test_db_conn, champion_id=99, champion_key="MonkeyKing", name="Wukong",
        tags=["Fighter"], icon_path="https://example.com/wukong.png", ddragon_version="14.13.1",
    )
    resolved = oracles_elixir._resolve_champion_id(test_db_conn, "Wukong")
    assert resolved == 99
