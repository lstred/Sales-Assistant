"""Budget / Forecast computation service.

Given prior-year blended sales, a map of CC → growth-percentage, and a
12-value monthly seasonality list (P1=Feb through P12=Jan), produces budget
rows at three levels of granularity:

  * By Cost Center (``compute_budget_by_cc``)
  * By Sales Rep   (``compute_budget_by_rep``)
  * By Account     (``compute_budget_by_account``)

Rep and account budgets are derived from each entity's share of prior-year
sales within their cost center(s), then multiplied by the same CC-level
growth factor and seasonality curve.
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


# ---------------------------------------------------------------- internal helpers

def _cc_aggregates(
    prior_df: pd.DataFrame | None,
    cc_growth_pct: dict[str, float],
    cc_names: dict[str, str],
) -> dict[str, dict]:
    """Aggregate prior_df by cost_center and compute CC-level budget amounts.

    Returns a dict  cc_code → {cc_code, cc_name, prior, growth_pct, budget,
                                dollar_change}
    for every CC that either has prior sales OR has a configured growth %.
    """
    result: dict[str, dict] = {}

    if prior_df is not None and not prior_df.empty:
        grouped = (
            prior_df.groupby("cost_center", as_index=False)["revenue"]
            .sum()
        )
        for _, row in grouped.iterrows():
            cc = str(row["cost_center"]).strip()
            if not cc:
                continue
            prior = float(row["revenue"] or 0)
            pct = cc_growth_pct.get(cc, 0.0)
            budget = prior * (1.0 + pct / 100.0)
            result[cc] = {
                "cc_code": cc,
                "cc_name": cc_names.get(cc, cc),
                "prior": prior,
                "growth_pct": pct,
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
) -> list[BudgetRow]:
    """One BudgetRow per cost center (codes starting with '0')."""
    cc_data = _cc_aggregates(prior_df, cc_growth_pct, cc_names)
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
) -> list[BudgetRow]:
    """One BudgetRow per (rep, cost_center) pair.

    Each rep's budget share within a CC is proportional to their prior-year
    sales in that CC (same logic as the account view, one level up).
    """
    cc_data = _cc_aggregates(prior_df, cc_growth_pct, cc_names)
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

    rows: list[BudgetRow] = []
    for cc, d in sorted(cc_data.items()):
        cc_budget = d["budget"]
        cc_prior = d["prior"]
        cc_monthly = _monthly_budget(cc_budget, seasonality_pct)

        # All reps with prior sales in this CC
        cc_reps = {
            num: prior
            for (num, c), prior in rep_cc_prior.items()
            if c == cc
        }

        if not cc_reps or cc_prior == 0:
            rows.append(
                BudgetRow(
                    cc_code=cc,
                    cc_name=d["cc_name"],
                    rep_number="",
                    rep_name="(unassigned)",
                    prior_year_sales=cc_prior,
                    growth_pct=d["growth_pct"],
                    dollar_change=d["dollar_change"],
                    budget_full_year=cc_budget,
                    monthly_budget=cc_monthly,
                )
            )
            continue

        for num, rep_prior in sorted(cc_reps.items()):
            share = rep_prior / cc_prior if cc_prior > 0 else 0.0
            rep_budget = cc_budget * share
            rows.append(
                BudgetRow(
                    cc_code=cc,
                    cc_name=d["cc_name"],
                    rep_number=num,
                    rep_name=rep_names.get(num, num),
                    prior_year_sales=rep_prior,
                    growth_pct=d["growth_pct"],
                    dollar_change=rep_budget - rep_prior,
                    budget_full_year=rep_budget,
                    monthly_budget=[m * share for m in cc_monthly],
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
) -> list[BudgetRow]:
    """One BudgetRow per (account, cost_center) pair."""
    cc_data = _cc_aggregates(prior_df, cc_growth_pct, cc_names)
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

    rows: list[BudgetRow] = []
    for cc, d in sorted(cc_data.items()):
        cc_budget = d["budget"]
        cc_prior = d["prior"]
        cc_monthly = _monthly_budget(cc_budget, seasonality_pct)

        cc_accts = {
            acct: prior
            for (acct, c), prior in acct_cc_prior.items()
            if c == cc
        }

        if not cc_accts or cc_prior == 0:
            rows.append(
                BudgetRow(
                    cc_code=cc,
                    cc_name=d["cc_name"],
                    prior_year_sales=cc_prior,
                    growth_pct=d["growth_pct"],
                    dollar_change=d["dollar_change"],
                    budget_full_year=cc_budget,
                    monthly_budget=cc_monthly,
                )
            )
            continue

        for acct, acct_prior in sorted(cc_accts.items()):
            share = acct_prior / cc_prior if cc_prior > 0 else 0.0
            acct_budget = cc_budget * share
            info = account_info.get(acct, {})
            rep_info = acct_rep.get((acct, cc), ("", ""))
            rows.append(
                BudgetRow(
                    cc_code=cc,
                    cc_name=d["cc_name"],
                    rep_number=rep_info[0],
                    rep_name=rep_info[1],
                    account_new=acct,
                    account_old=str(info.get("old", "") or "").strip(),
                    account_name=str(info.get("name", "") or "").strip().lstrip("*").strip(),
                    prior_year_sales=acct_prior,
                    growth_pct=d["growth_pct"],
                    dollar_change=acct_budget - acct_prior,
                    budget_full_year=acct_budget,
                    monthly_budget=[m * share for m in cc_monthly],
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
