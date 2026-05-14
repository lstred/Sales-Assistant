"""Per-month invoice cache.

Invoiced lines (``INVOICE# > 0``) are immutable once posted, so any
historical month's data can be cached forever in local SQLite. This
turns repeated date-range / CC-filter changes from multi-minute warehouse
hits into instant disk reads.

Cache strategy:

* Keyed by ``(year, month, code_prefix)``. ``code_prefix`` is the same
  ``"0"`` / ``""`` used by the loader; one cache slot per prefix so a
  product-only fetch never collides with an unfiltered one.
* ``cost_centers`` is intentionally *not* part of the key — we always
  fetch the full month for the prefix, then filter CCs in pandas after
  retrieval. This means changing the CC selection is free.
* The current calendar month is **never persisted** (invoices may still
  post into it). That month is fetched fresh every time.
* Future months are skipped entirely.
"""

from __future__ import annotations

import logging
import pickle
import sqlite3
from contextlib import closing
from datetime import date
from typing import Iterable

import pandas as pd

from app.app_paths import state_db_path
from app.config.models import DatabaseConfig
from app.data import queries
from app.data.db import read_dataframe

log = logging.getLogger(__name__)

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS invoice_month_cache_v3 (
    year         INTEGER NOT NULL,
    month        INTEGER NOT NULL,
    code_prefix  TEXT    NOT NULL,
    fetched_at   TEXT    NOT NULL,
    rows         INTEGER NOT NULL DEFAULT 0,
    payload      BLOB    NOT NULL,
    PRIMARY KEY (year, month, code_prefix)
)
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(state_db_path())
    conn.execute(_TABLE_DDL)
    return conn


def _month_iter(start: date, end: date) -> Iterable[tuple[int, int]]:
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1


def _month_bounds(year: int, month: int) -> tuple[int, int]:
    """Return ``(yyyymmdd_start, yyyymmdd_end)`` for the given month."""
    start = year * 10000 + month * 100 + 1
    if month == 12:
        next_first = (year + 1) * 10000 + 100 + 1
    else:
        next_first = year * 10000 + (month + 1) * 100 + 1
    end = next_first - 1
    # Compute "last day" properly by going one day back via date arithmetic.
    last_day = (date(year + (1 if month == 12 else 0),
                     1 if month == 12 else month + 1, 1)).toordinal() - 1
    last = date.fromordinal(last_day)
    end = last.year * 10000 + last.month * 100 + last.day
    return start, end


def _fetch_month(
    db: DatabaseConfig, year: int, month: int, code_prefix: str
) -> pd.DataFrame:
    s, e = _month_bounds(year, month)
    df = read_dataframe(
        db,
        queries.INVOICED_SALES_LINES,
        params={
            "start_yyyymmdd": s,
            "end_yyyymmdd": e,
            "cc_csv": "",
            "code_prefix": (code_prefix or "").strip(),
        },
    )
    return df


def _get_cached(year: int, month: int, code_prefix: str) -> pd.DataFrame | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT payload FROM invoice_month_cache_v3 "
            "WHERE year=? AND month=? AND code_prefix=?",
            (year, month, (code_prefix or "").strip()),
        ).fetchone()
    if not row:
        return None
    try:
        return pickle.loads(row[0])
    except Exception:  # noqa: BLE001 — corrupt → treat as miss
        return None


def _put_cached(year: int, month: int, code_prefix: str, df: pd.DataFrame) -> None:
    payload = pickle.dumps(df, protocol=pickle.HIGHEST_PROTOCOL)
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO invoice_month_cache_v3 "
            "(year, month, code_prefix, fetched_at, rows, payload) "
            "VALUES (?, ?, ?, datetime('now'), ?, ?)",
            (year, month, (code_prefix or "").strip(), int(len(df)), payload),
        )
        conn.commit()


def get_for_range(
    db: DatabaseConfig,
    start: date,
    end: date,
    code_prefix: str = "",
) -> pd.DataFrame:
    """Return invoiced-sales lines for ``[start, end]`` filtered by
    ``code_prefix``. Months wholly in the past are served from the
    persistent cache (and warmed on first miss); the current and future
    months are always fetched fresh."""
    today = date.today()
    cur_year, cur_month = today.year, today.month
    parts: list[pd.DataFrame] = []

    for y, m in _month_iter(start, end):
        if (y, m) > (cur_year, cur_month):
            continue  # nothing posted in the future
        is_immutable = (y, m) < (cur_year, cur_month)
        df: pd.DataFrame | None = None
        if is_immutable:
            df = _get_cached(y, m, code_prefix)
        if df is None:
            try:
                df = _fetch_month(db, y, m, code_prefix)
            except Exception:  # noqa: BLE001
                log.exception("invoice_cache: fetch failed for %d-%02d", y, m)
                df = pd.DataFrame()
            if is_immutable and df is not None:
                try:
                    _put_cached(y, m, code_prefix, df)
                except Exception:  # noqa: BLE001
                    log.exception("invoice_cache: write failed for %d-%02d", y, m)
        if df is not None and not df.empty:
            parts.append(df)

    if not parts:
        return pd.DataFrame(columns=[
            "invoice_yyyymmdd", "account_number", "cost_center",
            "salesperson_desc", "invoice_number", "order_number",
            "line_number", "revenue", "gross_profit",
        ])
    out = pd.concat(parts, ignore_index=True)
    # Trim to exact requested window (months are inclusive at the edges).
    s_int = start.year * 10000 + start.month * 100 + start.day
    e_int = end.year * 10000 + end.month * 100 + end.day
    out = out[(out["invoice_yyyymmdd"] >= s_int) & (out["invoice_yyyymmdd"] <= e_int)]
    return out.reset_index(drop=True)


def has_any() -> bool:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT COUNT(*) FROM invoice_month_cache_v3").fetchone()
    return bool(row and row[0])


def clear_all() -> int:
    with closing(_connect()) as conn:
        cur = conn.execute("DELETE FROM invoice_month_cache_v3")
        conn.commit()
        return int(cur.rowcount or 0)


def stats() -> tuple[int, int]:
    """Return ``(months_cached, total_rows)``."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(rows), 0) FROM invoice_month_cache_v3"
        ).fetchone()
    return (int(row[0] or 0), int(row[1] or 0))
