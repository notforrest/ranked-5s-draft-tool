-- Ranked 5s Draft Tool -- database schema.
-- Single source of truth for all tables. Applied fresh by scripts/init_db.py.
-- See /Users/forrestsun/.claude/plans/my-friends-and-i-nifty-wozniak.md for design rationale.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================
-- Roster
-- ============================================================
CREATE TABLE players (
    player_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name    TEXT NOT NULL,
    riot_game_name  TEXT NOT NULL,
    riot_tag_line   TEXT NOT NULL,
    puuid           TEXT UNIQUE,
    platform_region TEXT NOT NULL,
    account_region  TEXT NOT NULL,
    preferred_roles TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX idx_players_riotid ON players(riot_game_name, riot_tag_line);

CREATE TABLE game_sessions (
    session_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    our_side        TEXT,                        -- BLUE | RED, set once known (matchmaking-assigned)
    label           TEXT
);

CREATE TABLE session_lineup (
    session_id      INTEGER NOT NULL REFERENCES game_sessions(session_id),
    player_id       INTEGER NOT NULL REFERENCES players(player_id),
    assigned_role   TEXT NOT NULL,               -- TOP|JUNGLE|MID|BOTTOM|SUPPORT
    PRIMARY KEY (session_id, player_id)
);

-- ============================================================
-- Static champion data (Data Dragon / Community Dragon -- no API key needed)
-- ============================================================
CREATE TABLE champions (
    champion_id     INTEGER PRIMARY KEY,
    champion_key    TEXT NOT NULL UNIQUE,        -- DDragon internal key, e.g. "MonkeyKing"
    name            TEXT NOT NULL,                -- display name, e.g. "Wukong"
    tags            TEXT,                         -- JSON array of DDragon class tags, e.g. ["Fighter","Tank"] -- NOT lane roles
    icon_path       TEXT,
    ddragon_version TEXT NOT NULL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Empirically-derived lane-role eligibility (DDragon tags are class, not lane) -- rebuilt by aggregate jobs.
CREATE TABLE champion_role_eligibility (
    champion_id     INTEGER NOT NULL REFERENCES champions(champion_id),
    role            TEXT NOT NULL,                -- TOP|JUNGLE|MID|BOTTOM|SUPPORT
    source          TEXT NOT NULL,                -- 'personal' | 'pro'
    games_observed  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (champion_id, role, source)
);

-- ============================================================
-- Personal match-history cache (Match-V5, roster only)
-- ============================================================
CREATE TABLE matches (
    match_id         TEXT PRIMARY KEY,
    platform_region  TEXT NOT NULL,
    game_creation_ms INTEGER NOT NULL,
    game_duration_s  INTEGER,
    queue_id         INTEGER,
    game_mode        TEXT,
    patch            TEXT,
    raw_json_path    TEXT,
    fetched_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_matches_creation ON matches(game_creation_ms);

CREATE TABLE match_participants (
    match_id        TEXT NOT NULL REFERENCES matches(match_id),
    puuid           TEXT NOT NULL,
    player_id       INTEGER REFERENCES players(player_id),
    champion_id     INTEGER NOT NULL REFERENCES champions(champion_id),
    role            TEXT,                          -- normalized: TOP|JUNGLE|MID|BOTTOM|SUPPORT
    win             INTEGER NOT NULL,               -- 0/1
    kills INTEGER, deaths INTEGER, assists INTEGER,
    team_id         INTEGER,                        -- 100/200
    PRIMARY KEY (match_id, puuid)
);
CREATE INDEX idx_participants_player ON match_participants(player_id);
CREATE INDEX idx_participants_champion ON match_participants(champion_id);
CREATE INDEX idx_participants_match_team ON match_participants(match_id, team_id);

-- Ranked queue ids treated as "Ranked 5s-relevant" for personal aggregate stats.
-- 420 = Ranked Solo/Duo, 440 = Ranked Flex. Kept as a plain constant list, not a table,
-- since it's a fixed piece of domain knowledge, not user-editable data.

-- ============================================================
-- Personal per-champion aggregate stats (derived, rebuilt by aggregate job)
-- ============================================================
CREATE TABLE personal_champion_stats (
    player_id       INTEGER NOT NULL REFERENCES players(player_id),
    champion_id     INTEGER NOT NULL REFERENCES champions(champion_id),
    role            TEXT NOT NULL,
    games           INTEGER NOT NULL DEFAULT 0,
    wins            INTEGER NOT NULL DEFAULT 0,
    last_played_ms  INTEGER,
    computed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (player_id, champion_id, role)
);

-- ============================================================
-- Mastery (Champion-Mastery-V4) -- wholesale overwrite per player each refresh
-- ============================================================
CREATE TABLE champion_mastery (
    player_id                      INTEGER NOT NULL REFERENCES players(player_id),
    champion_id                    INTEGER NOT NULL REFERENCES champions(champion_id),
    champion_level                 INTEGER NOT NULL,
    champion_points                INTEGER NOT NULL,
    last_play_time_ms              INTEGER,
    champion_points_since_last_lvl INTEGER,
    champion_points_until_next_lvl INTEGER,
    fetched_at                     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (player_id, champion_id)
);

-- ============================================================
-- Curated global tier-list (hand-editable, imported from data/curated/tier_list/<patch>.yaml)
-- ============================================================
CREATE TABLE global_tier_list (
    champion_id     INTEGER NOT NULL REFERENCES champions(champion_id),
    role            TEXT NOT NULL,
    patch           TEXT NOT NULL,
    win_rate        REAL,
    pick_rate       REAL,
    ban_rate        REAL,
    tier            TEXT,
    sample_size     INTEGER,
    source_note     TEXT,
    imported_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (champion_id, role, patch)
);

-- ============================================================
-- Oracle's Elixir raw pro-game ingestion (never mutated post-import; re-import is idempotent upsert)
-- ============================================================
CREATE TABLE oe_games_raw (
    gameid           TEXT NOT NULL,
    playerid         TEXT,
    participantid    INTEGER,
    side             TEXT,                         -- 'Blue' | 'Red'
    position         TEXT,                         -- top|jng|mid|bot|sup|team
    playername       TEXT,
    teamname         TEXT,
    teamid           TEXT,
    champion_id      INTEGER REFERENCES champions(champion_id),  -- resolved from OE's champion name at import time
    champion_name_raw TEXT,                        -- OE's original name string, kept for debugging alias mismatches
    ban1 TEXT, ban2 TEXT, ban3 TEXT, ban4 TEXT, ban5 TEXT,
    result           INTEGER,                       -- 0/1
    league           TEXT,
    year             INTEGER,
    split            TEXT,
    playoffs         INTEGER,
    date             TEXT,
    patch            TEXT,
    gamelength       INTEGER,
    datacompleteness TEXT,                          -- 'complete' | 'partial' -- exclude 'partial' from synergy calcs
    imported_at      TEXT NOT NULL DEFAULT (datetime('now')),
    source_file      TEXT NOT NULL,
    PRIMARY KEY (gameid, participantid)
);
CREATE INDEX idx_oe_games_champion ON oe_games_raw(champion_id);
CREATE INDEX idx_oe_games_teamside ON oe_games_raw(gameid, teamid);

-- ============================================================
-- Derived pairwise aggregates: ally synergy (same team) and enemy matchup (opposing team)
-- Canonical ordering champion_id_a < champion_id_b for both synergy tables.
-- Matchup tables are directional-agnostic in storage (a/b are just "the pair"); the querying
-- code decides which side is "ours" vs "theirs" -- a matchup's win rate is stored as
-- champion_id_a's win rate against champion_id_b.
-- ============================================================
CREATE TABLE synergy_pro (
    champion_id_a   INTEGER NOT NULL REFERENCES champions(champion_id),
    champion_id_b   INTEGER NOT NULL REFERENCES champions(champion_id),
    games_together  INTEGER NOT NULL,
    wins_together   INTEGER NOT NULL,
    patch_scope     TEXT,
    computed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (champion_id_a, champion_id_b)
);

CREATE TABLE synergy_roster (
    champion_id_a   INTEGER NOT NULL REFERENCES champions(champion_id),
    champion_id_b   INTEGER NOT NULL REFERENCES champions(champion_id),
    games_together  INTEGER NOT NULL,
    wins_together   INTEGER NOT NULL,
    computed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (champion_id_a, champion_id_b)
);

-- champion_id_a's win rate when facing champion_id_b on the opposing team.
CREATE TABLE matchup_pro (
    champion_id_a   INTEGER NOT NULL REFERENCES champions(champion_id),
    champion_id_b   INTEGER NOT NULL REFERENCES champions(champion_id),
    games           INTEGER NOT NULL,
    wins_a          INTEGER NOT NULL,               -- games champion_id_a's team won
    patch_scope     TEXT,
    computed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (champion_id_a, champion_id_b)
);

CREATE TABLE matchup_roster (
    champion_id_a   INTEGER NOT NULL REFERENCES champions(champion_id),
    champion_id_b   INTEGER NOT NULL REFERENCES champions(champion_id),
    games           INTEGER NOT NULL,
    wins_a          INTEGER NOT NULL,
    computed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (champion_id_a, champion_id_b)
);

-- ============================================================
-- Operational bookkeeping
-- ============================================================
CREATE TABLE schema_migrations (
    version         INTEGER PRIMARY KEY,
    applied_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE refresh_state (
    player_id             INTEGER PRIMARY KEY REFERENCES players(player_id),
    last_match_fetched_ms INTEGER,
    last_refreshed_at     TEXT,
    last_refresh_status   TEXT,                     -- 'ok' | 'partial' | 'error'
    last_error_message    TEXT
);

CREATE TABLE refresh_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT,                            -- 'success' | 'partial' | 'failed'
    summary_json    TEXT
);

-- ============================================================
-- Live draft session persistence (state machine snapshot -- see draft/state.py)
-- Stored server-side too (not just browser localStorage) so a server restart mid-draft
-- doesn't lose state either; browser localStorage is the fast path, this is the durable one.
-- ============================================================
CREATE TABLE draft_sessions (
    draft_session_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       INTEGER REFERENCES game_sessions(session_id),
    our_side         TEXT NOT NULL,                  -- BLUE | RED
    current_slot     INTEGER NOT NULL DEFAULT 0,
    entries_json      TEXT NOT NULL DEFAULT '[]',     -- serialized 20-element entries array
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
