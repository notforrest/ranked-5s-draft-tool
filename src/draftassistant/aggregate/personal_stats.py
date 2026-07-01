"""Derived personal per-champion aggregate stats, rebuilt wholesale from match_participants
after every pre-draft refresh."""
from __future__ import annotations


def recompute_personal_stats(conn) -> None:
    """Rebuilds personal_champion_stats (games/wins/last_played_ms per player+champion+role) and
    champion_role_eligibility(source='personal') from match_participants, restricted to ranked
    queues.

    match_repo.get_all_roster_participants() already filters to roster players + ranked queues,
    but does not return game_creation_ms (that lives on the `matches` table) -- rather than adding
    a field to that shared repo contract, this function does its own small join directly via
    conn.execute() to pull the timestamps it needs for last_played_ms.
    """
    from draftassistant import config
    from draftassistant.db.repositories import champion_repo, match_repo, personal_stats_repo

    placeholders = ",".join("?" * len(config.RANKED_QUEUE_IDS))
    rows = conn.execute(
        f"""
        SELECT mp.player_id, mp.champion_id, mp.role, mp.win, m.game_creation_ms
        FROM match_participants mp
        JOIN matches m ON m.match_id = mp.match_id
        WHERE mp.player_id IS NOT NULL AND m.queue_id IN ({placeholders})
        """,
        config.RANKED_QUEUE_IDS,
    ).fetchall()

    stats: dict[tuple[int, int, str], list[int]] = {}  # (player_id, champion_id, role) -> [games, wins, last_played_ms]
    role_counts: dict[tuple[int, str], int] = {}  # (champion_id, role) -> games, for eligibility

    for r in rows:
        role = r["role"] or "UNKNOWN"
        key = (r["player_id"], r["champion_id"], role)
        entry = stats.setdefault(key, [0, 0, None])
        entry[0] += 1
        if r["win"]:
            entry[1] += 1
        creation = r["game_creation_ms"]
        if creation is not None and (entry[2] is None or creation > entry[2]):
            entry[2] = creation

        if r["role"]:
            role_key = (r["champion_id"], r["role"])
            role_counts[role_key] = role_counts.get(role_key, 0) + 1

    stats_rows = [
        (player_id, champion_id, role, games, wins, last_played_ms)
        for (player_id, champion_id, role), (games, wins, last_played_ms) in stats.items()
    ]
    personal_stats_repo.replace_all(conn, stats_rows)

    eligibility_rows = [(champ_id, role, games) for (champ_id, role), games in role_counts.items()]
    champion_repo.replace_role_eligibility(conn, "personal", eligibility_rows)
