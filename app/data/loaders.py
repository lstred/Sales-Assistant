"""High-level loader functions over the NRF_REPORTS warehouse."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

import pandas as pd

from app.config.models import DatabaseConfig
from app.data import queries
from app.data.db import read_dataframe
from app.services.fiscal_calendar import (
    build_fiscal_year,
    fiscal_year_for as _fy_for,
    period_for_invoice_yyyymmdd,
)

# First day the new system (dbo._ORDERS) became the source of truth for
# invoiced sales. Anything before this date lives only in the legacy
# ``ClydeMarketingHistory`` summarized table.
NEW_SYSTEM_CUTOFF: date = date(2025, 8, 4)
LEGACY_REP_LABEL: str = "(legacy / pre-Aug 2025)"


# ----------------------------------------------------------------- references
def load_cost_centers(db: DatabaseConfig) -> pd.DataFrame:
    """Cost-center reference (XREF view — only mapped product CCs)."""
    return read_dataframe(db, queries.COST_CENTER_XREF)


def load_all_cost_centers(db: DatabaseConfig) -> pd.DataFrame:
    """Master cost-center list from ``dbo.ITEM`` (includes sample ``1xx``
    CCs). Falls back to the XREF view if ITEM is empty for any reason."""
    df = read_dataframe(db, queries.ALL_COST_CENTERS)
    if df is None or df.empty:
        return load_cost_centers(db)
    return df


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


def load_blended_sales(
    db: DatabaseConfig,
    start: date,
    end: date,
    cost_centers: Iterable[str] | None = None,
    six_week_january_years: Iterable[int] = (),
) -> pd.DataFrame:
    """Sales between ``start`` and ``end`` blended across both systems.

    For dates ``>= NEW_SYSTEM_CUTOFF`` (2025-08-04) we use line-level
    invoiced sales from ``dbo._ORDERS``. For dates before the cutoff we
    fall back to the summarized ``dbo.ClydeMarketingHistory`` table,
    unpivoting its 12 monthly columns into one row per
    (account × cost center × fiscal period). Legacy rows have no rep
    attribution and are tagged with :data:`LEGACY_REP_LABEL`.

    Returns a DataFrame with these columns (always present):
    ``invoice_date``, ``fiscal_year``, ``fiscal_period``,
    ``fiscal_period_name``, ``account_number``, ``cost_center``,
    ``salesperson_desc``, ``revenue``, ``gross_profit``,
    ``invoice_number``, ``data_source``.
    """
    parts: list[pd.DataFrame] = []

    # New-system portion
    new_start = max(start, NEW_SYSTEM_CUTOFF)
    if new_start <= end:
        new_df = load_invoiced_sales(db, new_start, end, cost_centers)
        if new_df is not None and not new_df.empty:
            new_df = new_df.copy()
            new_df["data_source"] = "new"
            parts.append(new_df)

    # Legacy portion (everything before the cutoff in the requested range).
    # We attribute legacy rows to the *current* rep that owns each
    # (account × cost-center) pair via dbo.BILLSLMN, so YoY rep totals
    # stay consistent across the cutoff. Accounts with no current
    # assignment fall back to ``LEGACY_REP_LABEL``.
    if start < NEW_SYSTEM_CUTOFF:
        legacy_end = min(end, NEW_SYSTEM_CUTOFF - timedelta(days=1))
        fy_start = _fy_for(start)
        fy_end = _fy_for(legacy_end)
        try:
            old_raw = load_old_sales(db, fy_start, fy_end)
        except Exception:  # noqa: BLE001
            old_raw = pd.DataFrame()
        rep_map: dict[tuple[str, str], str] = {}
        try:
            assignments = load_rep_assignments(db)
            if assignments is not None and not assignments.empty:
                for rec in assignments.to_dict("records"):
                    acct = str(rec.get("account_number") or "").strip()
                    cc = str(rec.get("cost_center") or "").strip()
                    name = str(rec.get("salesman_name") or "").strip()
                    if acct and cc and name:
                        rep_map[(acct, cc)] = name
        except Exception:  # noqa: BLE001
            rep_map = {}
        if old_raw is not None and not old_raw.empty:
            legacy = _unpivot_old_sales(
                old_raw, start, legacy_end, cost_centers,
                six_week_january_years, rep_map,
            )
            if not legacy.empty:
                legacy["data_source"] = "legacy"
                parts.append(legacy)

    cols = [
        "invoice_date", "fiscal_year", "fiscal_period", "fiscal_period_name",
        "account_number", "cost_center", "salesperson_desc",
        "revenue", "gross_profit", "invoice_number", "data_source",
    ]
    if not parts:
        return pd.DataFrame(columns=cols)
    parts = [p.reindex(columns=cols) for p in parts]
    out = pd.concat(parts, ignore_index=True)
    # Ensure numeric dtypes for downstream aggregations
    for c in ("revenue", "gross_profit"):
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)
    return out


def _unpivot_old_sales(
    raw: pd.DataFrame,
    start: date,
    end: date,
    cost_centers: Iterable[str] | None,
    six_week_january_years: Iterable[int],
    rep_map: dict[tuple[str, str], str] | None = None,
) -> pd.DataFrame:
    """Unpivot ``ClydeMarketingHistory`` SalesPeriod1..12 / CostsPeriod1..12
    columns into one row per (account × cost center × fiscal period) within
    the requested date window.

    ``rep_map`` is an optional ``(account_number, cost_center) -> rep_name``
    lookup used to attribute legacy sales to the current owning rep.
    """
    if raw is None or raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    df["cost_center"] = df["cost_center"].astype(str).str.strip()
    df["account_number"] = df["account_number"].fillna("").astype(str).str.strip()

    if cost_centers:
        wanted = {str(c).strip() for c in cost_centers if c}
        if wanted:
            df = df[df["cost_center"].isin(wanted)]
    if df.empty:
        return df

    # Pre-build period boundaries per (fiscal_year) so we don't recompute.
    sw = list(six_week_january_years or ())
    fy_periods: dict[int, list] = {}
    for fy in df["fiscal_year"].dropna().astype(int).unique():
        fy_periods[int(fy)] = build_fiscal_year(int(fy), sw)

    rmap = rep_map or {}
    rows = []
    for rec in df.to_dict("records"):
        fy = int(rec.get("fiscal_year") or 0)
        if fy not in fy_periods:
            continue
        cc = rec.get("cost_center") or ""
        acct = rec.get("account_number") or ""
        rep_name = rmap.get((acct, cc), "") or LEGACY_REP_LABEL
        for i in range(1, 13):
            rev = rec.get(f"SalesPeriod{i}") or 0
            cost = rec.get(f"CostsPeriod{i}") or 0
            if not rev and not cost:
                continue
            p = fy_periods[fy][i - 1]
            if p.end < start or p.start > end:
                continue
            rev_f = float(rev or 0)
            cost_f = float(cost or 0)
            rows.append({
                "invoice_date": pd.Timestamp(p.start),
                "fiscal_year": fy,
                "fiscal_period": p.period,
                "fiscal_period_name": p.name,
                "account_number": acct,
                "cost_center": cc,
                "salesperson_desc": rep_name,
                "revenue": rev_f,
                "gross_profit": rev_f - cost_f,
                "invoice_number": None,
            })
    return pd.DataFrame(rows)


def load_open_orders(
    db: DatabaseConfig,
    cost_centers: Iterable[str] | None = None,
) -> pd.DataFrame:
    """Open (un-invoiced) order lines. Useful for pipeline insights only —
    never counted as salesman credit until the invoice posts."""
    cc_csv = ",".join(c for c in (cost_centers or ()) if c)
    return read_dataframe(db, queries.OPEN_ORDERS_LINES, params={"cc_csv": cc_csv})


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
