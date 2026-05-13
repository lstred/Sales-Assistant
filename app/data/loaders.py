"""High-level loader functions over the NRF_REPORTS warehouse.

Each loader applies the standard filters and column normalizations so the
service / UI layer can stay clean.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from app.config.models import DatabaseConfig
from app.data import queries
from app.data.db import read_dataframe


def load_cost_centers(db: DatabaseConfig) -> pd.DataFrame:
    """Cost-center reference (new code, name, old marketing code)."""
    return read_dataframe(db, queries.COST_CENTER_XREF)


def load_reps(db: DatabaseConfig) -> pd.DataFrame:
    """All sales reps from SALESMAN table."""
    return read_dataframe(db, queries.REPS_ROSTER)


def load_rep_assignments(db: DatabaseConfig) -> pd.DataFrame:
    """Rep ↔ account ↔ cost-center assignments (with closed-account flag)."""
    df = read_dataframe(db, queries.REP_ASSIGNMENTS)
    if "is_closed" in df.columns:
        df["is_closed"] = df["is_closed"].astype(bool)
    return df


def load_new_sales_monthly(
    db: DatabaseConfig,
    start: date,
    end: date,
) -> pd.DataFrame:
    """New-system sales aggregated to (account, cost_center, year, month)."""
    return read_dataframe(
        db,
        queries.NEW_SYSTEM_SALES_MONTHLY,
        params={
            "start_yyyymmdd": int(start.strftime("%Y%m%d")),
            "end_yyyymmdd": int(end.strftime("%Y%m%d")),
        },
    )


def load_old_sales(
    db: DatabaseConfig,
    fy_start: int,
    fy_end: int,
) -> pd.DataFrame:
    """Old-system summarized sales (ClydeMarketingHistory)."""
    return read_dataframe(
        db,
        queries.OLD_SYSTEM_SALES,
        params={"fy_start": fy_start, "fy_end": fy_end},
    )


def load_display_types(db: DatabaseConfig) -> pd.DataFrame:
    return read_dataframe(db, queries.DISPLAY_TYPES)


def load_display_placements(db: DatabaseConfig) -> pd.DataFrame:
    df = read_dataframe(db, queries.DISPLAY_PLACEMENTS)
    if "placed_on" in df.columns:
        df["placed_on"] = pd.to_datetime(df["placed_on"], errors="coerce")
    return df


# ----------------------------------------------------------------- fiscal year
def fiscal_year_for(d: date) -> int:
    """NRF fiscal year starts in February. Feb–Dec → calendar+1; Jan → calendar."""
    return d.year + 1 if d.month >= 2 else d.year
