"""Central config loaded from .env. Import settings from here rather than reading os.environ directly."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "lol_draft.db"
RAW_MATCHES_DIR = DATA_DIR / "raw" / "matches"
RAW_OE_DIR = DATA_DIR / "raw" / "oracles_elixir"
CURATED_TIER_LIST_DIR = DATA_DIR / "curated" / "tier_list"
DDRAGON_CACHE_DIR = DATA_DIR / "raw" / "ddragon_cache"

load_dotenv(PROJECT_ROOT / ".env")

RIOT_API_KEY = os.environ.get("RIOT_API_KEY", "")
RIOT_ACCOUNT_REGION = os.environ.get("RIOT_ACCOUNT_REGION", "americas")
RIOT_PLATFORM_REGION = os.environ.get("RIOT_PLATFORM_REGION", "na1")

# Optional: the current Oracle's Elixir CSV/Google-Sheets-export download link, grabbed by hand
# from https://oracleselixir.com/tools/downloads (the exact link isn't stable long-term, so this
# is a manually-set-once value, not something the app discovers on its own). When set, the setup
# screen's "Fetch Oracle's Elixir data" button uses it directly instead of requiring a file
# upload each time.
ORACLES_ELIXIR_CSV_URL = os.environ.get("ORACLES_ELIXIR_CSV_URL", "")

# Ranked queues treated as "Ranked 5s-relevant" for personal aggregate stats.
RANKED_QUEUE_IDS = (420, 440)  # 420 = Solo/Duo, 440 = Flex

# Upper bound on how many ranked match ids a single refresh will ever page through for one
# player (first-time backfill or incremental). This is a defensive ceiling against a
# pathological account, NOT the intended limiting factor -- match fetching pages through
# Match-V5 with type=ranked until Riot returns a short page (no more matches), and this cap is
# far larger than any realistic season's ranked game count, so it should never actually bind.
MATCH_HISTORY_SAFETY_CAP = 1000

VALID_ROLES = ("TOP", "JUNGLE", "MID", "BOTTOM", "SUPPORT")
VALID_ACCOUNT_REGIONS = ("americas", "asia", "europe")


def ensure_data_dirs() -> None:
    for d in (RAW_MATCHES_DIR, RAW_OE_DIR, CURATED_TIER_LIST_DIR, DDRAGON_CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)
