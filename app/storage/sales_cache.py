"""Persistent cache for blended sales DataFrames.

Saves the result of ``load_blended_sales`` keyed by query parameters so
that re-opening the app doesn't trigger a multi-minute warehouse hit on
every screen. Stored in the local SQLite app state DB under the
``sales_cache`` table.
"""

from __future__ import annotations

import io
import pickle
import sqlite3
from contextlib import closing
from datetime import date, datetime
from typing import Iterable

import pandas as pd

from app.app_paths import state_db_path

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS sales_cache (
    cache_key     TEXT PRIMARY KEY,
    refreshed_at  TEXT NOT NULL,
    rows          INTEGER NOT NULL DEFAULT 0,
    payload       BLOB NOT NULL
)
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(state_db_path())
    conn.execute(_TABLE_DDL)
    return conn


def make_key(
    start: date,
    end: date,
    cost_centers: Iterable[str] | None,
    code_prefix: str = "",
) -> str:
    ccs = ",".join(sorted({str(c).strip() for c in (cost_centers or ()) if c}))
    prefix = (code_prefix or "").strip()
    return f"{start.isoformat()}|{end.isoformat()}|{ccs}|p={prefix}"


def get(key: str) -> tuple[pd.DataFrame, datetime] | None:
    """Return ``(df, refreshed_at)`` if cached, else ``None``."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT refreshed_at, payload FROM sales_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
    if not row:
        return None
    refreshed_at_str, payload = row
    try:
        df = pickle.loads(payload)
    except Exception:  # noqa: BLE001 — corrupt cache, treat as miss
        return None
    try:
        ts = datetime.fromisoformat(refreshed_at_str)
    except ValueError:
        ts = datetime.utcnow()
    return df, ts


def put(key: str, df: pd.DataFrame) -> datetime:
    """Cache ``df`` under ``key`` and return the refresh timestamp."""
    ts = datetime.now()
    payload = pickle.dumps(df, protocol=pickle.HIGHEST_PROTOCOL)
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sales_cache "
            "(cache_key, refreshed_at, rows, payload) VALUES (?, ?, ?, ?)",
            (key, ts.isoformat(timespec="seconds"), int(len(df) if df is not None else 0), payload),
        )
        conn.commit()
    return ts


def latest_refresh() -> datetime | None:
    """Return the most recent refresh timestamp across all cache entries."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT MAX(refreshed_at) FROM sales_cache"
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except ValueError:
        return None


def has_any() -> bool:
    return latest_refresh() is not None


def clear_all() -> int:
    """Delete every cached entry. Returns the number of rows removed."""
    with closing(_connect()) as conn:
        cur = conn.execute("DELETE FROM sales_cache")
        conn.commit()
        return cur.rowcount or 0
