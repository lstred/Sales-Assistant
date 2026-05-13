"""SQLite connection for local app state."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from app.app_paths import state_db_path
from app.storage.schema import CURRENT_SCHEMA_VERSION, SCHEMA_STATEMENTS


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(state_db_path(), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Apply schema (create-if-not-exists)."""
    with get_conn() as conn:
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)
        existing = conn.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if not existing or existing["version"] < CURRENT_SCHEMA_VERSION:
            conn.execute(
                "INSERT OR REPLACE INTO schema_version(version, applied_at) VALUES (?, ?)",
                (CURRENT_SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
            )
