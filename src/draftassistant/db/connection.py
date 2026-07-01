"""Single place that opens SQLite connections. WAL mode so live-draft reads are
never blocked by a refresh job's writes."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from draftassistant import config


def get_conn() -> sqlite3.Connection:
    # check_same_thread=False: FastAPI's sync dependencies/endpoints run in a worker
    # threadpool that doesn't guarantee the same OS thread handles a request's dependency
    # setup and its endpoint body. Each request still gets its own fresh connection here
    # (opened once per request, closed at the end of it) -- this isn't sharing a connection
    # across concurrent requests, just relaxing sqlite3's same-thread check for the single
    # request-scoped connection that FastAPI's threadpool may touch from more than one thread.
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def session():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
