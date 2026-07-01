import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "src" / "draftassistant" / "db" / "schema.sql"


@pytest.fixture
def test_db_conn():
    """A fresh in-memory SQLite DB with the real schema applied. Use this in any test that
    touches the database instead of the real data/lol_draft.db."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())
    yield conn
    conn.close()
