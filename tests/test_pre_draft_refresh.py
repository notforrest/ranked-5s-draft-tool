"""Tests for the pre-draft refresh orchestrator against a mocked Riot API surface (respx)."""
from __future__ import annotations

import json

import httpx
import respx

from draftassistant import config
from draftassistant.db.repositories import champion_repo, match_repo, roster_repo
from draftassistant.refresh import pre_draft_refresh

PUUID = "fake-puuid-0001"
MATCH_ID = "NA1_9999999999"


def _seed_player(conn, *, display_name="TestPlayer", riot_game_name="TestPlayer",
                  riot_tag_line="NA1") -> int:
    return roster_repo.upsert_player(
        conn, display_name=display_name, riot_game_name=riot_game_name, riot_tag_line=riot_tag_line,
        platform_region="na1", account_region="americas",
    )


def _seed_champion(conn, champion_id: int = 1, name: str = "Ahri") -> None:
    champion_repo.upsert_champion(
        conn, champion_id=champion_id, champion_key=name, name=name, tags=["Mage"],
        icon_path=f"https://example.com/{name}.png", ddragon_version="14.13.1",
    )


def _mock_account_endpoint(game_name="TestPlayer", tag_line="NA1", puuid=PUUID):
    respx.get(
        f"https://americas.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
    ).mock(return_value=httpx.Response(200, json={"puuid": puuid, "gameName": game_name, "tagLine": tag_line}))


def _mock_mastery_endpoint(puuid=PUUID, entries=None):
    if entries is None:
        entries = [
            {"championId": 1, "championLevel": 7, "championPoints": 123456,
             "lastPlayTime": 1700000000000, "championPointsSinceLastLevel": 100,
             "championPointsUntilNextLevel": 0},
        ]
    respx.get(
        f"https://na1.api.riotgames.com/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}"
    ).mock(return_value=httpx.Response(200, json=entries))


def _mock_match_ids_endpoint(puuid=PUUID, match_ids=None):
    if match_ids is None:
        match_ids = [MATCH_ID]
    respx.get(
        url__regex=rf"https://americas\.api\.riotgames\.com/lol/match/v5/matches/by-puuid/{puuid}/ids.*"
    ).mock(return_value=httpx.Response(200, json=match_ids))


def _match_detail_payload(match_id=MATCH_ID, puuid=PUUID, other_puuid="other-puuid-999",
                           game_creation_ms=1700000500000, queue_id=420, win=True):
    return {
        "metadata": {"matchId": match_id},
        "info": {
            "gameCreation": game_creation_ms,
            "gameDuration": 1800,
            "queueId": queue_id,
            "gameMode": "CLASSIC",
            "gameVersion": "14.13.567.891",
            "participants": [
                {
                    "puuid": puuid, "championId": 1, "teamPosition": "MIDDLE", "win": win,
                    "kills": 5, "deaths": 2, "assists": 8, "teamId": 100,
                },
                {
                    "puuid": other_puuid, "championId": 2, "teamPosition": "TOP", "win": not win,
                    "kills": 1, "deaths": 4, "assists": 2, "teamId": 200,
                },
            ],
        },
    }


def _mock_match_detail_endpoint(match_id=MATCH_ID, payload=None):
    if payload is None:
        payload = _match_detail_payload(match_id=match_id)
    respx.get(f"https://americas.api.riotgames.com/lol/match/v5/matches/{match_id}").mock(
        return_value=httpx.Response(200, json=payload)
    )
    return payload


def test_full_refresh_happy_path(test_db_conn, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RAW_MATCHES_DIR", tmp_path / "matches")
    monkeypatch.setattr(config, "RIOT_API_KEY", "RGAPI-fake")

    player_id = _seed_player(test_db_conn)
    _seed_champion(test_db_conn, champion_id=1, name="Ahri")
    _seed_champion(test_db_conn, champion_id=2, name="LeeSin")
    test_db_conn.commit()

    with respx.mock:
        _mock_account_endpoint()
        _mock_mastery_endpoint()
        _mock_match_ids_endpoint()
        _mock_match_detail_endpoint()

        summary = pre_draft_refresh.run(test_db_conn)

    assert summary["status"] == "success"
    assert summary["fatal_auth_error"] is None
    assert len(summary["players"]) == 1
    player_summary = summary["players"][0]
    assert player_summary["status"] == "ok"
    assert player_summary["new_matches_fetched"] == 1

    # puuid resolved and persisted.
    player_row = roster_repo.get_player(test_db_conn, player_id)
    assert player_row["puuid"] == PUUID

    # Mastery persisted.
    mastery_rows = test_db_conn.execute(
        "SELECT * FROM champion_mastery WHERE player_id = ?", (player_id,)
    ).fetchall()
    assert len(mastery_rows) == 1
    assert mastery_rows[0]["champion_id"] == 1

    # Match cached to disk.
    cache_path = (tmp_path / "matches") / f"{MATCH_ID}.json"
    assert cache_path.exists()
    cached_json = json.loads(cache_path.read_text())
    assert cached_json["metadata"]["matchId"] == MATCH_ID

    # matches row inserted with derived patch.
    match_row = test_db_conn.execute(
        "SELECT * FROM matches WHERE match_id = ?", (MATCH_ID,)
    ).fetchone()
    assert match_row is not None
    assert match_row["patch"] == "14.13"
    assert match_row["queue_id"] == 420

    # match_participants contains ONLY the roster puuid, not the "other" participant.
    participant_rows = test_db_conn.execute(
        "SELECT * FROM match_participants WHERE match_id = ?", (MATCH_ID,)
    ).fetchall()
    assert len(participant_rows) == 1
    assert participant_rows[0]["puuid"] == PUUID
    assert participant_rows[0]["player_id"] == player_id
    assert participant_rows[0]["role"] == "MID"
    assert participant_rows[0]["win"] == 1

    # refresh_state watermark advanced to the match's gameCreation.
    refresh_state = match_repo.get_refresh_state(test_db_conn, player_id)
    assert refresh_state["last_match_fetched_ms"] == 1700000500000
    assert refresh_state["last_refresh_status"] == "ok"

    # personal_champion_stats populated by the post-loop aggregate recompute.
    stats_rows = test_db_conn.execute("SELECT * FROM personal_champion_stats").fetchall()
    assert len(stats_rows) == 1
    assert stats_rows[0]["player_id"] == player_id
    assert stats_rows[0]["champion_id"] == 1
    assert stats_rows[0]["games"] == 1
    assert stats_rows[0]["wins"] == 1


def test_incremental_refresh_skips_already_fetched_match(test_db_conn, tmp_path, monkeypatch):
    """Re-running the refresh with the same match id already in the DB should not re-fetch match
    detail (match_exists short-circuits it), and match_ids should still be queried using the
    stored watermark."""
    monkeypatch.setattr(config, "RAW_MATCHES_DIR", tmp_path / "matches")
    monkeypatch.setattr(config, "RIOT_API_KEY", "RGAPI-fake")

    player_id = _seed_player(test_db_conn)
    _seed_champion(test_db_conn, champion_id=1, name="Ahri")
    _seed_champion(test_db_conn, champion_id=2, name="LeeSin")
    test_db_conn.commit()

    with respx.mock:
        _mock_account_endpoint()
        _mock_mastery_endpoint()
        _mock_match_ids_endpoint()
        _mock_match_detail_endpoint()
        pre_draft_refresh.run(test_db_conn)

    # Second run: match id list still returns the same match, but match detail must NOT be
    # requested again (route not mocked at all this time -- respx.mock() with no matching route
    # raises, so if the code tried to fetch it again this test would fail on that call).
    with respx.mock:
        _mock_account_endpoint()
        _mock_mastery_endpoint()
        _mock_match_ids_endpoint()
        summary = pre_draft_refresh.run(test_db_conn)

    assert summary["status"] == "success"
    assert summary["players"][0]["new_matches_fetched"] == 0

    match_rows = test_db_conn.execute("SELECT * FROM matches").fetchall()
    assert len(match_rows) == 1  # not duplicated


def test_first_player_auth_error_short_circuits_run(test_db_conn, tmp_path, monkeypatch):
    """A 401 on the very first player must stop the whole run immediately -- no other players
    should be touched, and the run's overall status must be 'failed'."""
    monkeypatch.setattr(config, "RAW_MATCHES_DIR", tmp_path / "matches")
    monkeypatch.setattr(config, "RIOT_API_KEY", "RGAPI-expired")

    player1_id = _seed_player(test_db_conn, display_name="Alice", riot_game_name="Alice", riot_tag_line="NA1")
    player2_id = _seed_player(test_db_conn, display_name="Bob", riot_game_name="Bob", riot_tag_line="NA1")
    test_db_conn.commit()

    with respx.mock:
        respx.get(
            "https://americas.api.riotgames.com/riot/account/v1/accounts/by-riot-id/Alice/NA1"
        ).mock(return_value=httpx.Response(401, json={"status": {"message": "Unauthorized", "status_code": 401}}))
        # Bob's account endpoint deliberately NOT mocked -- if the code tries to call it, this
        # test fails because respx has no matching route registered.

        summary = pre_draft_refresh.run(test_db_conn)

    assert summary["status"] == "failed"
    assert summary["fatal_auth_error"] is not None
    assert len(summary["players"]) == 1  # only Alice was attempted
    assert summary["players"][0]["status"] == "error"

    refresh_state = match_repo.get_refresh_state(test_db_conn, player1_id)
    assert refresh_state["last_refresh_status"] == "error"

    # Bob was never touched at all.
    bob_state = match_repo.get_refresh_state(test_db_conn, player2_id)
    assert bob_state is None
    bob_row = roster_repo.get_player(test_db_conn, player2_id)
    assert bob_row["puuid"] is None

    # The refresh_runs row should reflect the failure too.
    latest_run = match_repo.get_latest_refresh_run(test_db_conn)
    assert latest_run["status"] == "failed"


def test_second_player_auth_error_does_not_stop_run(test_db_conn, tmp_path, monkeypatch):
    """An auth error on a LATER player (not the first) should be recorded and the run should
    continue -- only a first-player auth error is treated as fatal-for-everyone."""
    monkeypatch.setattr(config, "RAW_MATCHES_DIR", tmp_path / "matches")
    monkeypatch.setattr(config, "RIOT_API_KEY", "RGAPI-fake")

    _seed_player(test_db_conn, display_name="Alice", riot_game_name="Alice", riot_tag_line="NA1")
    player2_id = _seed_player(test_db_conn, display_name="Bob", riot_game_name="Bob", riot_tag_line="NA1")
    _seed_champion(test_db_conn, champion_id=1, name="Ahri")
    _seed_champion(test_db_conn, champion_id=2, name="LeeSin")
    test_db_conn.commit()

    with respx.mock:
        _mock_account_endpoint(game_name="Alice", tag_line="NA1", puuid="alice-puuid")
        respx.get(
            f"https://na1.api.riotgames.com/lol/champion-mastery/v4/champion-masteries/by-puuid/alice-puuid"
        ).mock(return_value=httpx.Response(200, json=[]))
        respx.get(
            url__regex=r"https://americas\.api\.riotgames\.com/lol/match/v5/matches/by-puuid/alice-puuid/ids.*"
        ).mock(return_value=httpx.Response(200, json=[]))

        respx.get(
            "https://americas.api.riotgames.com/riot/account/v1/accounts/by-riot-id/Bob/NA1"
        ).mock(return_value=httpx.Response(403, json={"status": {"message": "Forbidden", "status_code": 403}}))

        summary = pre_draft_refresh.run(test_db_conn)

    assert summary["status"] == "partial"
    assert summary["fatal_auth_error"] is None
    assert len(summary["players"]) == 2
    assert summary["players"][0]["status"] == "ok"
    assert summary["players"][1]["status"] == "error"

    bob_state = match_repo.get_refresh_state(test_db_conn, player2_id)
    assert bob_state["last_refresh_status"] == "error"


def test_unexpected_error_on_one_player_does_not_abort_the_batch(test_db_conn, tmp_path, monkeypatch):
    """A non-Riot-specific failure for one player (e.g. a malformed API response, or -- the
    motivating real case -- a stale duplicate row causing a puuid UNIQUE-constraint violation)
    must not crash the whole run: it should be isolated and reported like the Riot-specific
    error cases, with every other player still refreshed normally."""
    monkeypatch.setattr(config, "RAW_MATCHES_DIR", tmp_path / "matches")
    monkeypatch.setattr(config, "RIOT_API_KEY", "RGAPI-fake")

    player1_id = _seed_player(test_db_conn, display_name="Alice", riot_game_name="Alice", riot_tag_line="NA1")
    player2_id = _seed_player(test_db_conn, display_name="Bob", riot_game_name="Bob", riot_tag_line="NA1")
    _seed_champion(test_db_conn, champion_id=1, name="Ahri")
    _seed_champion(test_db_conn, champion_id=2, name="LeeSin")
    test_db_conn.commit()

    with respx.mock:
        _mock_account_endpoint(game_name="Alice", tag_line="NA1", puuid="alice-puuid")
        respx.get(
            "https://na1.api.riotgames.com/lol/champion-mastery/v4/champion-masteries/by-puuid/alice-puuid"
        ).mock(return_value=httpx.Response(200, json=[]))
        respx.get(
            url__regex=r"https://americas\.api\.riotgames\.com/lol/match/v5/matches/by-puuid/alice-puuid/ids.*"
        ).mock(return_value=httpx.Response(200, json=[]))

        # Bob's account resolves fine, but his mastery endpoint returns a malformed body (null
        # instead of a list) -- this raises a plain TypeError deep inside mastery_repo, not one
        # of the Riot-specific exception types the earlier except-clauses know about.
        _mock_account_endpoint(game_name="Bob", tag_line="NA1", puuid="bob-puuid")
        respx.get(
            "https://na1.api.riotgames.com/lol/champion-mastery/v4/champion-masteries/by-puuid/bob-puuid"
        ).mock(return_value=httpx.Response(200, json=None))

        summary = pre_draft_refresh.run(test_db_conn)

    assert summary["status"] == "partial"
    assert summary["fatal_auth_error"] is None
    assert len(summary["players"]) == 2
    assert summary["players"][0]["status"] == "ok"
    assert summary["players"][1]["status"] == "error"
    assert "Unexpected error" in summary["players"][1]["error"]

    # Alice was refreshed normally despite Bob's failure.
    alice_row = roster_repo.get_player(test_db_conn, player1_id)
    assert alice_row["puuid"] == "alice-puuid"

    # Bob's puuid WAS resolved before the mastery call blew up (resolution happens first) and
    # his failure is recorded, but he didn't take Alice down with him.
    bob_row = roster_repo.get_player(test_db_conn, player2_id)
    assert bob_row["puuid"] == "bob-puuid"
    bob_state = match_repo.get_refresh_state(test_db_conn, player2_id)
    assert bob_state["last_refresh_status"] == "error"

    latest_run = match_repo.get_latest_refresh_run(test_db_conn)
    assert latest_run["status"] == "partial"
