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
    code_prefix: str = "",
) -> pd.DataFrame:
    """Line-level invoiced sales between ``start`` and ``end`` inclusive.

    Pass ``cost_centers`` to filter to one/more/all CCs (None or empty
    iterable = all CCs).

    ``code_prefix`` (e.g. ``"0"``) restricts to cost centers whose code
    starts with that prefix; this is **always** applied even when
    ``cost_centers`` is empty so sample CCs (``'1xx'``) never leak into
    product-only views.

    Closed historical months are served from the local invoice cache
    (``app.storage.invoice_cache``); only the current calendar month is
    fetched fresh from the warehouse.

    Adds derived columns:
    * ``invoice_date`` (datetime)
    * ``fiscal_year``, ``fiscal_period``, ``fiscal_period_name``
    """
    from app.storage import invoice_cache

    df = invoice_cache.get_for_range(db, start, end, code_prefix or "")
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()

    # Apply optional CC filter post-fetch (cache stores all CCs for the prefix).
    ccs = [str(c).strip() for c in (cost_centers or ()) if c]
    if ccs:
        df = df[df["cost_center"].astype(str).str.strip().isin(ccs)]
        if df.empty:
            return df

    df = df.copy()
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
    code_prefix: str = "",
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
    ``price_class``, ``salesperson_desc``, ``revenue``, ``gross_profit``,
    ``invoice_number``, ``data_source``.

    Sales attribution is always driven by the **current** rep-account
    assignment in ``dbo.BILLSLMN`` (source of truth). If an order line's
    ``SALESPERSON_DESC`` belongs to a rep who has since left and their
    accounts have been reassigned, the sales are credited to the current
    owner. Lines for accounts with no current BILLSLMN entry keep their
    original ``SALESPERSON_DESC``.
    """
    parts: list[pd.DataFrame] = []
    prefix = (code_prefix or "").strip()

    # Always build the rep attribution map from the current BILLSLMN
    # assignments so BOTH new-system and legacy rows are attributed to
    # whoever owns the account TODAY — not to whoever placed the order.
    assignments_df_: pd.DataFrame | None = None
    try:
        assignments_df_ = load_rep_assignments(db)
    except Exception:  # noqa: BLE001
        pass

    # Build two structures from the assignments:
    # 1. A DataFrame keyed by (account_number, cost_center) -> salesman_name
    #    for a fast vectorized merge onto the sales data.
    # 2. The same map as a dict for use in the legacy unpivot loop.
    rep_map: dict[tuple[str, str], str] = {}
    rep_map_df: pd.DataFrame | None = None
    if assignments_df_ is not None and not assignments_df_.empty:
        ax = assignments_df_.copy()
        ax["account_number"] = ax["account_number"].fillna("").astype(str).str.strip()
        ax["cost_center"] = ax["cost_center"].fillna("").astype(str).str.strip()
        ax["salesman_name"] = ax["salesman_name"].fillna("").astype(str).str.strip()
        ax = ax[ax["salesman_name"] != ""][["account_number", "cost_center", "salesman_name"]]
        ax = ax.drop_duplicates(subset=["account_number", "cost_center"])
        rep_map_df = ax.reset_index(drop=True)
        rep_map = {
            (r["account_number"], r["cost_center"]): r["salesman_name"]
            for r in ax.to_dict("records")
        }

    # New-system portion
    new_start = max(start, NEW_SYSTEM_CUTOFF)
    if new_start <= end:
        new_df = load_invoiced_sales(db, new_start, end, cost_centers, prefix)
        if new_df is not None and not new_df.empty:
            new_df = new_df.copy()
            # Re-attribute each line to the CURRENT account owner per BILLSLMN
            # using a vectorized merge.
            #
            # Index note: load_invoiced_sales may return a boolean-filtered
            # slice of the per-month cache, giving a non-sequential index
            # (e.g. rows 0, 5, 12…). After the left-merge, `merged` gets a
            # fresh RangeIndex (0..N-1). We MUST reset new_df's index first
            # so that the final assignment aligns correctly; otherwise
            # override.where(…, orig) would use pandas label-alignment and
            # produce NaN everywhere the fallback fires.
            if rep_map_df is not None and not rep_map_df.empty:
                new_df = new_df.reset_index(drop=True)
                new_df["account_number"] = (
                    new_df["account_number"].fillna("").astype(str).str.strip()
                )
                new_df["cost_center"] = (
                    new_df["cost_center"].fillna("").astype(str).str.strip()
                )
                merged = new_df.merge(
                    rep_map_df,
                    on=["account_number", "cost_center"],
                    how="left",
                )
                # `merged` has RangeIndex 0..N-1 and same row order as new_df.
                # Use merged["salesperson_desc"] as fallback so index is aligned.
                orig = merged["salesperson_desc"].fillna("").astype(str).str.strip()
                override = merged["salesman_name"].fillna("").astype(str).str.strip()
                new_df["salesperson_desc"] = override.where(override != "", orig)
            new_df["data_source"] = "new"
            parts.append(new_df)

    # Legacy portion (everything before the cutoff in the requested range).
    # The rep_map built above is reused so legacy rows get the same
    # BILLSLMN-driven attribution as new-system rows.
    if start < NEW_SYSTEM_CUTOFF:
        legacy_end = min(end, NEW_SYSTEM_CUTOFF - timedelta(days=1))
        fy_start = _fy_for(start)
        fy_end = _fy_for(legacy_end)
        try:
            old_raw = load_old_sales(db, fy_start, fy_end)
        except Exception:  # noqa: BLE001
            old_raw = pd.DataFrame()
        if old_raw is not None and not old_raw.empty:
            legacy = _unpivot_old_sales(
                old_raw, start, legacy_end, cost_centers,
                six_week_january_years, rep_map, prefix,
            )
            if not legacy.empty:
                legacy["data_source"] = "legacy"
                parts.append(legacy)

    cols = [
        "invoice_date", "fiscal_year", "fiscal_period", "fiscal_period_name",
        "account_number", "cost_center", "price_class", "salesperson_desc",
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
    code_prefix: str = "",
) -> pd.DataFrame:
    """Unpivot ``ClydeMarketingHistory`` SalesPeriod1..12 / CostsPeriod1..12
    columns into one row per (account × cost center × fiscal period) within
    the requested date window.

    ``rep_map`` is an optional ``(account_number, cost_center) -> rep_name``
    lookup used to attribute legacy sales to the current owning rep.
    ``code_prefix`` restricts to cost centers whose code starts with that
    prefix (applied alongside any explicit ``cost_centers`` filter).
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
    prefix = (code_prefix or "").strip()
    if prefix:
        df = df[df["cost_center"].str.startswith(prefix)]
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
    code_prefix: str = "",
) -> pd.DataFrame:
    """Open (un-invoiced) order lines. Useful for pipeline insights only —
    never counted as salesman credit until the invoice posts."""
    cc_csv = ",".join(c for c in (cost_centers or ()) if c)
    return read_dataframe(
        db,
        queries.OPEN_ORDERS_LINES,
        params={"cc_csv": cc_csv, "code_prefix": (code_prefix or "").strip()},
    )


# ----------------------------------------------------------------- displays
def load_display_types(db: DatabaseConfig) -> pd.DataFrame:
    return read_dataframe(db, queries.DISPLAY_TYPES)


def load_display_placements(db: DatabaseConfig) -> pd.DataFrame:
    df = read_dataframe(db, queries.DISPLAY_PLACEMENTS)
    if "placed_on" in df.columns:
        df["placed_on"] = pd.to_datetime(df["placed_on"], errors="coerce")
    return df


def load_price_class_lookup(db: DatabaseConfig) -> dict[str, str]:
    """Return a ``{price_class_code: description}`` mapping from ``dbo.PRICE``.

    Used to enrich rep scorecards and AI prompts with product-type context.
    Falls back to an empty dict on any error so callers never crash.
    """
    try:
        df = read_dataframe(db, queries.PRICE_CLASS_LOOKUP)
        if df is None or df.empty:
            return {}
        result: dict[str, str] = {}
        for code, desc in zip(df["price_class"], df["price_class_desc"]):
            code_s = str(code).strip() if code is not None else ""
            if not code_s:
                continue
            # Skip NaN / None / blank descriptions — caller falls back to the code.
            if pd.isna(desc) or not str(desc).strip():
                continue
            result[code_s] = str(desc).strip()
        return result
    except Exception:  # noqa: BLE001
        return {}


# ----------------------------------------------------------------- helpers
def fiscal_year_for(d: date) -> int:
    """Calendar date → fiscal year. Re-exported for back-compat."""
    return _fy_for(d)
