"""Tests for scripts/migrate_db.py -- adding schema.sql's new tables to an existing database
without wiping data, since init_db.py refuses to touch one that already exists."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "src" / "draftassistant" / "db" / "schema.sql"
SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "migrate_db.py"


def _load_migrate_module():
    spec = importlib.util.spec_from_file_location("migrate_db", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tables(db_path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_make_idempotent_adds_if_not_exists_to_create_statements():
    migrate_db = _load_migrate_module()
    original = "CREATE TABLE foo (id INTEGER);\nCREATE UNIQUE INDEX idx_foo ON foo(id);\nCREATE INDEX idx_bar ON foo(id);"
    rewritten = migrate_db._make_idempotent(original)
    assert "CREATE TABLE IF NOT EXISTS foo" in rewritten
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_foo" in rewritten
    assert "CREATE INDEX IF NOT EXISTS idx_bar" in rewritten
    # Applying it twice must not double up "IF NOT EXISTS" (would be invalid SQL).
    twice = migrate_db._make_idempotent(rewritten)
    assert "IF NOT EXISTS IF NOT EXISTS" not in twice


def test_migrate_adds_missing_tables_without_touching_existing_data(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"

    # Simulate a database created from an OLDER schema.sql missing the newest tables, by
    # applying the current schema.sql minus its 3 newest tables.
    old_schema = SCHEMA_PATH.read_text()
    for table_sql_start in ("CREATE TABLE synergy_opgg", "CREATE TABLE matchup_opgg", "CREATE TABLE summoner_rank"):
        start = old_schema.index(table_sql_start)
        end = old_schema.index(");", start) + len(");")
        old_schema = old_schema[:start] + old_schema[end:]

    conn = sqlite3.connect(db_path)
    conn.executescript(old_schema)
    conn.execute(
        "INSERT INTO champions (champion_id, champion_key, name, ddragon_version) VALUES (1, 'Ahri', 'Ahri', '14.13.1')"
    )
    conn.commit()
    conn.close()

    before = _tables(db_path)
    assert "summoner_rank" not in before

    migrate_db = _load_migrate_module()
    monkeypatch.setattr(migrate_db.config, "DB_PATH", db_path)
    migrate_db.main()

    after = _tables(db_path)
    assert {"summoner_rank", "matchup_opgg", "synergy_opgg"} <= after

    conn = sqlite3.connect(db_path)
    try:
        # Pre-existing data must survive untouched.
        assert conn.execute("SELECT name FROM champions WHERE champion_id = 1").fetchone() == ("Ahri",)
    finally:
        conn.close()


def test_migrate_is_a_safe_no_op_when_schema_already_current(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    conn.close()

    migrate_db = _load_migrate_module()
    monkeypatch.setattr(migrate_db.config, "DB_PATH", db_path)

    before = _tables(db_path)
    migrate_db.main()
    after = _tables(db_path)
    assert before == after


def test_migrate_exits_cleanly_when_no_database_exists(tmp_path, monkeypatch, capsys):
    migrate_db = _load_migrate_module()
    monkeypatch.setattr(migrate_db.config, "DB_PATH", tmp_path / "does_not_exist.db")

    with pytest.raises(SystemExit):
        migrate_db.main()
    assert "run scripts/init_db.py instead" in capsys.readouterr().out
