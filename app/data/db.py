"""SQL Server engine + helpers for the NRF_REPORTS warehouse.

Always use the provided helpers and parameterized queries; never f-string
user values into SQL.
"""

from __future__ import annotations

import logging
from urllib.parse import quote_plus

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.config.models import DatabaseConfig

log = logging.getLogger(__name__)

_engine_cache: dict[str, Engine] = {}


def get_engine(db: DatabaseConfig) -> Engine:
    """Return a cached SQLAlchemy engine for the given DB config."""
    odbc = db.odbc_connection_string()
    if odbc not in _engine_cache:
        url = f"mssql+pyodbc:///?odbc_connect={quote_plus(odbc)}"
        _engine_cache[odbc] = create_engine(
            url,
            fast_executemany=True,
            pool_pre_ping=True,
            pool_recycle=1800,
        )
    return _engine_cache[odbc]


def reset_engine_cache() -> None:
    for eng in _engine_cache.values():
        try:
            eng.dispose()
        except Exception:  # noqa: BLE001
            pass
    _engine_cache.clear()


def ping(db: DatabaseConfig) -> tuple[bool, str]:
    """Lightweight connection test. Returns (ok, message)."""
    try:
        eng = get_engine(db)
        with eng.connect() as conn:
            row = conn.execute(
                text("SELECT DB_NAME() AS db, SYSTEM_USER AS usr")
            ).fetchone()
        if row is None:
            return False, "Connected but received no row from SELECT."
        return True, f"Connected to {row[0]} as {row[1]}"
    except Exception as exc:  # noqa: BLE001
        log.exception("DB ping failed")
        return False, f"{type(exc).__name__}: {exc}"


def read_dataframe(
    db: DatabaseConfig,
    sql: str,
    params: dict | None = None,
) -> pd.DataFrame:
    """Execute parameterized SQL and return a DataFrame."""
    eng = get_engine(db)
    with eng.connect() as conn:
        return pd.read_sql_query(text(sql), conn, params=params or {})
