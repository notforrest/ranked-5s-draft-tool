"""FastAPI dependencies shared across routers."""
from __future__ import annotations

import sqlite3
from typing import Iterator

from draftassistant.db.connection import get_conn


def get_db() -> Iterator[sqlite3.Connection]:
    """Yields a DB connection for the lifetime of one request, closing it after.

    Standard FastAPI `Depends` generator pattern -- the connection is closed in the
    `finally` block whether the request handler succeeds or raises."""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
