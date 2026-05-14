"""Budget / Forecast computation service.

Given prior-year blended sales, growth percentages, and a 12-value monthly
seasonality list (P1=Feb through P12=Jan), this service produces budget rows
at three levels of granularity:

  * By Cost Center  (``compute_budget_by_cc``)
  * By Sales Rep    (``compute_budget_by_rep``)
  * By Account      (``compute_budget_by_account``)

Two growth-percentage modes are supported — they can be mixed:

  1. **CC-level fallback** (``cc_growth_pct: dict[str, float]``):
     A single growth % applied to every rep in that CC.  Used as the
     default when no rep-level override exists.

  2. **Rep+CC override** (``rep_cc_growth_pct: dict[tuple[str,str], float]``):
     Per-(rep_number, cc_code) growth %.  When present, this takes
     priority over the CC-level fallback for that rep.  The CC-level
     budget becomes the *sum* of its individual rep budgets.

Upload format for rep-level overrides (CSV or Excel):
  Columns (case-insensitive, whitespace-trimmed):
    • ``rep_number``   — SALESMAN.YSLMN# (e.g. ``42``)
    • ``cost_center``  — CC code (e.g. ``010``)
    • ``growth_pct``   — numeric growth or decline % (e.g. ``10`` for +10 %,
                         ``-5`` for −5 %)
  One row per rep × CC combination.  Missing combinations fall back to the
  CC-level growth % from the manual table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import pandas as pd

# Fiscal month names P1..P12 (Feb..Jan)
PERIOD_MONTH_NAMES: tuple[str, ...] = (
    "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December", "January",
)


@dataclass
class BudgetRow:
    """One aggregated budget row — can represent a CC, rep, or account."""

    cc_code: str = ""
    cc_name: str = ""
    rep_number: str = ""
    rep_name: str = ""
    account_new: str = ""       # new-system account number
    account_old: str = ""       # BBANK2
    account_name: str = ""

    prior_year_sales: float = 0.0   # baseline (prior FY actual)
    growth_pct: float = 0.0         # configured growth %
    dollar_change: float = 0.0      # budget − prior
    budget_full_year: float = 0.0   # prior × (1 + growth_pct/100)

    # 12 monthly budgets P1(Feb)..P12(Jan)
    monthly_budget: list[float] = field(default_factory=lambda: [0.0] * 12)

    # YTD fields — populated by add_ytd_actuals()
    prior_ytd_sales: float = 0.0    # prior year same-periods actual
    ytd_actual: float = 0.0         # current year actual through last completed period
    ytd_budget: float = 0.0         # budget for completed periods (from seasonality)
    vs_budget: float = 0.0          # ytd_actual − ytd_budget


# ---------------------------------------------------------------- upload helper

def parse_rep_cc_upload(path: str) -> tuple[dict[tuple[str, str], float], list[str]]:
    """Parse a CSV or Excel file with rep-level growth overrides.

    Expected columns (case-insensitive): ``rep_number``, ``cost_center``,
    ``growth_pct``.  Returns ``({(rep_number, cc): pct}, errors)`` where
    ``errors`` is a list of human-readable problems found during parsing.
    """
    import os
    errors: list[str] = []
    result: dict[tuple[str, str], float] = {}
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext in (".xlsx", ".xls"):
            df = pd.read_excel(path, dtype=str)
        else:
            df = pd.read_csv(path, dtype=str)
    except Exception as exc:  # noqa: BLE001
        return {}, [f"Could not open file: {exc}"]

    # Normalize column names
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    missing = [c for c in ("rep_number", "cost_center", "growth_pct") if c not in df.columns]
    if missing:
        return {}, [
            f"Missing required column(s): {', '.join(missing)}. "
            "File must have columns: rep_number, cost_center, growth_pct"
        ]

    for i, row in df.iterrows():
        raw_rep = row.get("rep_number", "")
        raw_cc = row.get("cost_center", "")
        raw_pct = row.get("growth_pct", "")
        # pandas reads empty cells as NaN even with dtype=str
        rep = "" if pd.isna(raw_rep) else str(raw_rep).strip()
        cc = "" if pd.isna(raw_cc) else str(raw_cc).strip()
        pct_str = "" if pd.isna(raw_pct) else str(raw_pct).strip().replace("%", "")
        if not rep or not cc:
            errors.append(f"Row {i + 2}: rep_number or cost_center is blank — skipped.")
            continue
        try:
            pct = float(pct_str)
        except ValueError:
            errors.append(f"Row {i + 2}: growth_pct '{pct_str}' is not a number — skipped.")
            continue
        result[(rep, cc)] = pct

    return result, errors


# ---------------------------------------------------------------- internal helpers

def _effective_growth(
    rep_num: str,
    cc: str,
    rep_cc_growth_pct: dict[tuple[str, str], float],
    cc_growth_pct: dict[str, float],
) -> float:
    """Return the growth % to apply: rep-CC override if present, else CC default."""
    return rep_cc_growth_pct.get((rep_num, cc), cc_growth_pct.get(cc, 0.0))


def _cc_aggregates(
    prior_df: pd.DataFrame | None,
    cc_growth_pct: dict[str, float],
    cc_names: dict[str, str],
    rep_cc_growth_pct: dict[tuple[str, str], float] | None = None,
    acct_rep_map: dict[tuple[str, str], tuple[str, str]] | None = None,
) -> dict[str, dict]:
    """Aggregate prior_df by cost_center and compute CC-level budget amounts.

    When rep_cc_growth_pct is provided the CC budget is the weighted sum of
    per-rep budgets (each rep's prior sales × their individual growth factor).
    Otherwise the simpler CC-level factor applies uniformly.

    Returns a dict  cc_code → {cc_code, cc_name, prior, growth_pct, budget,
                                dollar_change}
    where ``growth_pct`` is the *effective blended* rate at CC level.
    """
    rep_overrides = rep_cc_growth_pct or {}
    result: dict[str, dict] = {}

    if prior_df is not None and not prior_df.empty:
        grouped = (
            prior_df.groupby("cost_center", as_index=False)["revenue"].sum()
        )
        for _, row in grouped.iterrows():
            cc = str(row["cost_center"]).strip()
            if not cc:
                continue
            prior = float(row["revenue"] or 0)

            if rep_overrides and acct_rep_map:
                # Build rep-level prior sums for this CC
                rep_prior: dict[str, float] = {}
                for r in prior_df[prior_df["cost_center"] == cc].itertuples(index=False):
                    acct = str(getattr(r, "account_number", "") or "").strip()
                    info = acct_rep_map.get((acct, cc))
                    if info:
                        num = info[0]
                        rep_prior[num] = rep_prior.get(num, 0.0) + float(getattr(r, "revenue", 0) or 0)

                budget = sum(
                    rp * (1.0 + _effective_growth(num, cc, rep_overrides, cc_growth_pct) / 100.0)
                    for num, rp in rep_prior.items()
                )
                # For unassigned lines, use CC-level factor
                unassigned_prior = prior - sum(rep_prior.values())
                if unassigned_prior > 0:
                    budget += unassigned_prior * (1.0 + cc_growth_pct.get(cc, 0.0) / 100.0)
                blended_pct = (budget / prior - 1.0) * 100.0 if prior > 0 else cc_growth_pct.get(cc, 0.0)
            else:
                pct = cc_growth_pct.get(cc, 0.0)
                budget = prior * (1.0 + pct / 100.0)
                blended_pct = pct

            result[cc] = {
                "cc_code": cc,
                "cc_name": cc_names.get(cc, cc),
                "prior": prior,
                "growth_pct": blended_pct,
                "budget": budget,
                "dollar_change": budget - prior,
            }

    # Include CCs that have growth % but no prior sales
    for cc, pct in cc_growth_pct.items():
        if cc not in result:
            result[cc] = {
                "cc_code": cc,
                "cc_name": cc_names.get(cc, cc),
                "prior": 0.0,
                "growth_pct": pct,
                "budget": 0.0,
                "dollar_change": 0.0,
            }
    return result


def _monthly_budget(cc_budget: float, seasonality_pct: Sequence[float]) -> list[float]:
    return [cc_budget * (s / 100.0) for s in seasonality_pct]


def _build_acct_rep_map(
    assignments_df: pd.DataFrame | None,
) -> dict[tuple[str, str], tuple[str, str]]:
    """(account_number, cost_center) → (salesman_number, salesman_name)."""
    out: dict[tuple[str, str], tuple[str, str]] = {}
    if assignments_df is None or assignments_df.empty:
        return out
    for r in assignments_df.itertuples(index=False):
        acct = str(getattr(r, "account_number", "") or "").strip()
        cc = str(getattr(r, "cost_center", "") or "").strip()
        num = str(getattr(r, "salesman_number", "") or "").strip()
        name = str(getattr(r, "salesman_name", "") or "").strip()
        if acct and cc and num:
            out[(acct, cc)] = (num, name)
    return out


# ---------------------------------------------------------------- public API

def compute_budget_by_cc(
    prior_df: pd.DataFrame | None,
    cc_growth_pct: dict[str, float],
    seasonality_pct: Sequence[float],
    cc_names: dict[str, str],
    rep_cc_growth_pct: dict[tuple[str, str], float] | None = None,
    assignments_df: pd.DataFrame | None = None,
) -> list[BudgetRow]:
    """One BudgetRow per cost center.

    When ``rep_cc_growth_pct`` is provided the CC budget is the sum of
    per-rep budgets within the CC.
    """
    acct_rep = _build_acct_rep_map(assignments_df) if rep_cc_growth_pct else None
    cc_data = _cc_aggregates(prior_df, cc_growth_pct, cc_names, rep_cc_growth_pct, acct_rep)
    rows: list[BudgetRow] = []
    for cc, d in sorted(cc_data.items()):
        budget = d["budget"]
        rows.append(
            BudgetRow(
                cc_code=cc,
                cc_name=d["cc_name"],
                prior_year_sales=d["prior"],
                growth_pct=d["growth_pct"],
                dollar_change=d["dollar_change"],
                budget_full_year=budget,
                monthly_budget=_monthly_budget(budget, seasonality_pct),
            )
        )
    return rows


def compute_budget_by_rep(
    prior_df: pd.DataFrame | None,
    assignments_df: pd.DataFrame | None,
    cc_growth_pct: dict[str, float],
    seasonality_pct: Sequence[float],
    cc_names: dict[str, str],
    rep_cc_growth_pct: dict[tuple[str, str], float] | None = None,
) -> list[BudgetRow]:
    """One BudgetRow per (rep, cost_center) pair.

    Each rep's budget = their prior-year sales in that CC × their effective
    growth % (rep-level override first, CC-level fallback second).
    """
    rep_overrides = rep_cc_growth_pct or {}
    acct_rep = _build_acct_rep_map(assignments_df)

    # Attribute prior sales to reps via account ownership
    rep_cc_prior: dict[tuple[str, str], float] = {}   # (rep_num, cc) → prior $
    rep_names: dict[str, str] = {}
    if prior_df is not None and not prior_df.empty:
        for r in prior_df.itertuples(index=False):
            acct = str(getattr(r, "account_number", "") or "").strip()
            cc = str(getattr(r, "cost_center", "") or "").strip()
            rev = float(getattr(r, "revenue", 0) or 0)
            rep_info = acct_rep.get((acct, cc))
            if rep_info:
                num, name = rep_info
                rep_cc_prior[(num, cc)] = rep_cc_prior.get((num, cc), 0.0) + rev
                rep_names[num] = name

    # Collect all CCs that appear either in prior sales or in cc_growth_pct
    all_ccs = sorted(
        {cc for (_, cc) in rep_cc_prior}
        | set(cc_growth_pct.keys())
        | {cc for (_, cc) in rep_overrides}
    )

    rows: list[BudgetRow] = []
    for cc in all_ccs:
        cc_rep_priors = {
            num: prior
            for (num, c), prior in rep_cc_prior.items()
            if c == cc
        }
        cc_prior_total = sum(cc_rep_priors.values())

        if not cc_rep_priors:
            # No prior sales — use CC-level factor for an empty placeholder
            pct = cc_growth_pct.get(cc, 0.0)
            rows.append(
                BudgetRow(
                    cc_code=cc,
                    cc_name=cc_names.get(cc, cc),
                    rep_number="",
                    rep_name="(unassigned)",
                    prior_year_sales=0.0,
                    growth_pct=pct,
                    dollar_change=0.0,
                    budget_full_year=0.0,
                    monthly_budget=[0.0] * 12,
                )
            )
            continue

        for num, rep_prior in sorted(cc_rep_priors.items()):
            pct = _effective_growth(num, cc, rep_overrides, cc_growth_pct)
            rep_budget = rep_prior * (1.0 + pct / 100.0)
            rep_monthly = _monthly_budget(rep_budget, seasonality_pct)
            rows.append(
                BudgetRow(
                    cc_code=cc,
                    cc_name=cc_names.get(cc, cc),
                    rep_number=num,
                    rep_name=rep_names.get(num, num),
                    prior_year_sales=rep_prior,
                    growth_pct=pct,
                    dollar_change=rep_budget - rep_prior,
                    budget_full_year=rep_budget,
                    monthly_budget=rep_monthly,
                )
            )
    return rows


def compute_budget_by_account(
    prior_df: pd.DataFrame | None,
    assignments_df: pd.DataFrame | None,
    account_info: dict[str, dict],   # new_acct → {old, name}
    cc_growth_pct: dict[str, float],
    seasonality_pct: Sequence[float],
    cc_names: dict[str, str],
    rep_cc_growth_pct: dict[tuple[str, str], float] | None = None,
) -> list[BudgetRow]:
    """One BudgetRow per (account, cost_center) pair.

    Each account's budget inherits its rep's effective growth % (rep-level
    override if present, CC-level fallback otherwise).
    """
    rep_overrides = rep_cc_growth_pct or {}
    acct_rep = _build_acct_rep_map(assignments_df)

    # Aggregate prior sales by (account, cost_center)
    acct_cc_prior: dict[tuple[str, str], float] = {}
    if prior_df is not None and not prior_df.empty:
        for r in prior_df.itertuples(index=False):
            acct = str(getattr(r, "account_number", "") or "").strip()
            cc = str(getattr(r, "cost_center", "") or "").strip()
            rev = float(getattr(r, "revenue", 0) or 0)
            if acct and cc:
                acct_cc_prior[(acct, cc)] = acct_cc_prior.get((acct, cc), 0.0) + rev

    # Group by CC
    from collections import defaultdict
    cc_accts: dict[str, dict[str, float]] = defaultdict(dict)
    for (acct, cc), prior in acct_cc_prior.items():
        cc_accts[cc][acct] = prior

    all_ccs = sorted(
        set(cc_accts.keys()) | set(cc_growth_pct.keys())
        | {cc for (_, cc) in rep_overrides}
    )

    rows: list[BudgetRow] = []
    for cc in all_ccs:
        accts_in_cc = cc_accts.get(cc, {})

        if not accts_in_cc:
            pct = cc_growth_pct.get(cc, 0.0)
            rows.append(
                BudgetRow(
                    cc_code=cc,
                    cc_name=cc_names.get(cc, cc),
                    prior_year_sales=0.0,
                    growth_pct=pct,
                    dollar_change=0.0,
                    budget_full_year=0.0,
                    monthly_budget=[0.0] * 12,
                )
            )
            continue

        for acct, acct_prior in sorted(accts_in_cc.items()):
            rep_info = acct_rep.get((acct, cc), ("", ""))
            rep_num = rep_info[0]
            pct = _effective_growth(rep_num, cc, rep_overrides, cc_growth_pct)
            acct_budget = acct_prior * (1.0 + pct / 100.0)
            acct_monthly = _monthly_budget(acct_budget, seasonality_pct)
            info = account_info.get(acct, {})
            rows.append(
                BudgetRow(
                    cc_code=cc,
                    cc_name=cc_names.get(cc, cc),
                    rep_number=rep_num,
                    rep_name=rep_info[1],
                    account_new=acct,
                    account_old=str(info.get("old", "") or "").strip(),
                    account_name=str(info.get("name", "") or "").strip().lstrip("*").strip(),
                    prior_year_sales=acct_prior,
                    growth_pct=pct,
                    dollar_change=acct_budget - acct_prior,
                    budget_full_year=acct_budget,
                    monthly_budget=acct_monthly,
                )
            )
    return rows


def add_ytd_actuals(
    rows: list[BudgetRow],
    curr_ytd_df: pd.DataFrame | None,
    prior_ytd_df: pd.DataFrame | None,
    completed_period_indices: list[int],  # 0-based P1..P12 indices
    group_by: str = "cc",                 # "cc", "rep", or "account"
) -> None:
    """Mutate rows in-place: set ytd_budget, ytd_actual, prior_ytd_sales, vs_budget.

    YTD budget = sum of monthly_budget for completed periods.
    YTD actual  = actual current-FY invoiced sales, distributed by prior-year share.
    Prior YTD   = prior-year sales for the same period numbers (filtered from prior_ytd_df).
    """
    if not rows:
        return

    # YTD budget is purely from the monthly seasonality — no DB needed
    for row in rows:
        row.ytd_budget = sum(
            row.monthly_budget[i]
            for i in completed_period_indices
            if 0 <= i < len(row.monthly_budget)
        )

    # Current YTD and prior YTD — distribute by prior-year CC share
    curr_by_cc: dict[str, float] = {}
    prior_by_cc: dict[str, float] = {}
    if curr_ytd_df is not None and not curr_ytd_df.empty:
        curr_by_cc = curr_ytd_df.groupby("cost_center")["revenue"].sum().to_dict()
    if prior_ytd_df is not None and not prior_ytd_df.empty:
        prior_by_cc = prior_ytd_df.groupby("cost_center")["revenue"].sum().to_dict()

    # CC-level totals for proportional distribution
    cc_prior_total: dict[str, float] = {}
    for row in rows:
        cc_prior_total[row.cc_code] = (
            cc_prior_total.get(row.cc_code, 0.0) + row.prior_year_sales
        )

    if group_by == "cc":
        for row in rows:
            row.ytd_actual = float(curr_by_cc.get(row.cc_code, 0.0))
            row.prior_ytd_sales = float(prior_by_cc.get(row.cc_code, 0.0))
    else:
        # Distribute YTD actuals proportionally by entity's share of CC prior sales
        for row in rows:
            cc_total = cc_prior_total.get(row.cc_code, 0.0)
            share = row.prior_year_sales / cc_total if cc_total > 0 else 0.0
            row.ytd_actual = float(curr_by_cc.get(row.cc_code, 0.0)) * share
            row.prior_ytd_sales = float(prior_by_cc.get(row.cc_code, 0.0)) * share

    for row in rows:
        row.vs_budget = row.ytd_actual - row.ytd_budget


# ---------------------------------------------------------------- DataFrame export helpers

def rows_to_dataframe(
    rows: list[BudgetRow],
    mode: str,      # "cc", "rep", or "account"
    include_monthly: bool = True,
    include_ytd: bool = True,
) -> pd.DataFrame:
    """Convert BudgetRow list → a display/export DataFrame."""
    records = []
    for r in rows:
        rec: dict = {}
        if mode == "account":
            rec["New Account #"] = r.account_new
            rec["Legacy Acct # (BBANK2)"] = r.account_old
            rec["Account Name"] = r.account_name
        if mode in ("rep", "account"):
            rec["Rep #"] = r.rep_number
            rec["Rep Name"] = r.rep_name
        rec["CC #"] = r.cc_code
        rec["CC Name"] = r.cc_name
        rec["Prior Year Sales"] = r.prior_year_sales
        rec["Growth %"] = r.growth_pct
        rec["$ Change"] = r.dollar_change
        rec["Budget Full Year"] = r.budget_full_year
        if include_monthly:
            for i, name in enumerate(PERIOD_MONTH_NAMES):
                rec[f"{name} Budget"] = r.monthly_budget[i] if i < len(r.monthly_budget) else 0.0
        if include_ytd:
            rec["Prior YTD Sales"] = r.prior_ytd_sales
            rec["YTD Budget"] = r.ytd_budget
            rec["YTD Actual"] = r.ytd_actual
            rec["Vs Budget"] = r.vs_budget
        records.append(rec)
    return pd.DataFrame(records)
