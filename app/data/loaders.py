"""High-level loader functions over the NRF_REPORTS warehouse."""

from __future__ import annotations

from datetime import date
from typing import Iterable

import pandas as pd

from app.config.models import DatabaseConfig
from app.data import queries
from app.data.db import read_dataframe
from app.services.fiscal_calendar import (
    fiscal_year_for as _fy_for,
    period_for_invoice_yyyymmdd,
)


# ----------------------------------------------------------------- references
def load_cost_centers(db: DatabaseConfig) -> pd.DataFrame:
    """Cost-center reference (new code, name, old marketing code)."""
    return read_dataframe(db, queries.COST_CENTER_XREF)


def load_reps(db: DatabaseConfig) -> pd.DataFrame:
    return read_dataframe(db, queries.REPS_ROSTER)


def load_rep_assignments(db: DatabaseConfig) -> pd.DataFrame:
    df = read_dataframe(db, queries.REP_ASSIGNMENTS)
    if "is_closed" in df.columns:
        df["is_closed"] = df["is_closed"].astype(bool)
    return df


# ----------------------------------------------------------------- sales
def load_invoiced_sales(
    db: DatabaseConfig,
    start: date,
    end: date,
    cost_centers: Iterable[str] | None = None,
) -> pd.DataFrame:
    """Line-level invoiced sales between ``start`` and ``end`` inclusive.

    Pass ``cost_centers`` to filter to one/more/all CCs (None or empty
    iterable = all CCs).

    Adds derived columns:
    * ``invoice_date`` (datetime)
    * ``fiscal_year``, ``fiscal_period``, ``fiscal_period_name``
    """
    cc_csv = ",".join(c for c in (cost_centers or ()) if c)
    df = read_dataframe(
        db,
        queries.INVOICED_SALES_LINES,
        params={
            "start_yyyymmdd": int(start.strftime("%Y%m%d")),
            "end_yyyymmdd": int(end.strftime("%Y%m%d")),
            "cc_csv": cc_csv,
        },
    )
    if df.empty:
        return df
    df["invoice_date"] = pd.to_datetime(
        df["invoice_yyyymmdd"].astype("Int64").astype(str), format="%Y%m%d", errors="coerce"
    )
    # Fiscal period via the calendar service (one call per unique date)
    unique_days = df["invoice_yyyymmdd"].dropna().unique()
    cache: dict[int, tuple[int, int, str]] = {}
    for v in unique_days:
        try:
            p = period_for_invoice_yyyymmdd(int(v))
            cache[int(v)] = (p.fiscal_year, p.period, p.name)
        except ValueError:
            cache[int(v)] = (0, 0, "")
    df["fiscal_year"] = df["invoice_yyyymmdd"].map(lambda v: cache.get(int(v), (0,))[0])
    df["fiscal_period"] = df["invoice_yyyymmdd"].map(lambda v: cache.get(int(v), (0, 0))[1])
    df["fiscal_period_name"] = df["invoice_yyyymmdd"].map(lambda v: cache.get(int(v), (0, 0, ""))[2])
    return df


def load_old_sales(
    db: DatabaseConfig,
    fy_start: int,
    fy_end: int,
) -> pd.DataFrame:
    return read_dataframe(
        db,
        queries.OLD_SYSTEM_SALES,
        params={"fy_start": fy_start, "fy_end": fy_end},
    )


# ----------------------------------------------------------------- displays
def load_display_types(db: DatabaseConfig) -> pd.DataFrame:
    return read_dataframe(db, queries.DISPLAY_TYPES)


def load_display_placements(db: DatabaseConfig) -> pd.DataFrame:
    df = read_dataframe(db, queries.DISPLAY_PLACEMENTS)
    if "placed_on" in df.columns:
        df["placed_on"] = pd.to_datetime(df["placed_on"], errors="coerce")
    return df


# ----------------------------------------------------------------- helpers
def fiscal_year_for(d: date) -> int:
    """Calendar date → fiscal year. Re-exported for back-compat."""
    return _fy_for(d)
