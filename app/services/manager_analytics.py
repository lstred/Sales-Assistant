"""Sales-manager analytics: turn raw blended sales / displays / sample
DataFrames into rep scorecards, peer comparisons, period overviews, and the
structured context block the AI uses to draft personalised coaching emails.

Design rules:
* Every metric here is **deterministic** and computed in pandas — the AI
  consumes the result, it does not have to invent the numbers.
* Anything that depends on a "good enough" sample size is explicitly
  filtered (e.g. a rep with 2 accounts is not compared against a rep with
  150). Constants near the top of the file.
* Closed accounts (``account_name`` starts with ``*``) are excluded from
  penalty-style metrics but kept as historical context where relevant.
* All results are JSON-friendly (plain ``dict`` / ``list`` / ``float``) so
  they round-trip cleanly into the AI prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from typing import Iterable

import pandas as pd

# ----------------------------------------------------------------- thresholds
# A rep needs at least this many active accounts before peer comparisons fire.
MIN_ACCOUNTS_FOR_PEER_COMPARE = 5
# A rep needs at least this much revenue in the period before YoY % is shown.
MIN_REVENUE_FOR_YOY = 1_000.0
# A rep whose YoY swings beyond this threshold (in either direction) is
# almost always the result of an account-territory transfer, not real
# performance — we exclude them from the peer-average calculation and the
# email is steered toward more stable metrics (absolute revenue, GP%,
# 3-month momentum, account mix).
OUTLIER_YOY_PCT_THRESHOLD = 500.0
# An account counts as "active" if it had any invoiced revenue in the window.
# An account counts as "stale" if it had revenue in the prior window but
# zero in the current window — these are the highest-priority talking points.
TOP_N_ACCOUNTS = 5


# ----------------------------------------------------------------- dataclasses
@dataclass
class RepScorecard:
    rep_key: str
    rep_name: str
    revenue: float = 0.0
    prior_revenue: float = 0.0
    yoy_pct: float | None = None       # None when not enough signal
    peer_avg_yoy_pct: float | None = None
    vs_peers_pct: float | None = None  # rep YoY minus peer-avg YoY
    gross_profit: float = 0.0
    gpp_pct: float = 0.0
    invoice_lines: int = 0
    total_accounts: int = 0
    active_accounts: int = 0
    active_account_pct: float = 0.0
    accounts_with_core_displays: int = 0
    core_display_coverage_pct: float = 0.0
    sample_lines: int = 0
    sample_revenue: float = 0.0
    samples_per_account: float = 0.0
    last_3mo_revenue: float = 0.0
    prior_3mo_revenue: float = 0.0          # the 3 months immediately before
    last_3mo_vs_prior_3mo_pct: float | None = None
    last_3mo_yoy_pct: float | None = None   # vs same 3 months last year
    top_growing_accounts: list[dict] = field(default_factory=list)
    top_declining_accounts: list[dict] = field(default_factory=list)
    stale_accounts: list[dict] = field(default_factory=list)  # had sales last yr, zero now
    new_accounts: list[dict] = field(default_factory=list)    # zero last yr, sales now
    rank_revenue: int | None = None         # 1-based rank within peer set
    rank_yoy: int | None = None
    peer_count: int = 0
    is_yoy_outlier: bool = False            # True when YoY is so extreme it
                                            # would skew peer comparisons
    price_class_top: list[dict] = field(default_factory=list)  # top price classes
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class PeriodOverview:
    label: str                       # "FY27 P3 (April)" / "FY26 Q4" / "FY26"
    start: date
    end: date
    revenue: float = 0.0
    prior_revenue: float = 0.0
    yoy_pct: float | None = None
    gross_profit: float = 0.0
    gpp_pct: float = 0.0
    invoice_lines: int = 0
    active_reps: int = 0
    active_accounts: int = 0
    top_reps: list[dict] = field(default_factory=list)
    bottom_reps: list[dict] = field(default_factory=list)
    top_growing_ccs: list[dict] = field(default_factory=list)
    top_declining_ccs: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["start"] = self.start.isoformat()
        d["end"] = self.end.isoformat()
        return d


# ----------------------------------------------------------------- helpers
def _safe_pct(numer: float, denom: float) -> float | None:
    if not denom:
        return None
    return (numer / denom) * 100.0


def _yoy_pct(curr: float, prior: float) -> float | None:
    if prior is None or prior <= 0:
        return None
    return ((curr - prior) / prior) * 100.0


def _normalise_sales(df: pd.DataFrame | None) -> pd.DataFrame:
    """Coerce a blended-sales DataFrame to the shape the analytics expect.

    Always returns a copy with: ``rep_key``, ``account_number``,
    ``cost_center``, ``revenue``, ``gross_profit`` columns.
    """
    if df is None or df.empty:
        return pd.DataFrame(
            columns=[
                "invoice_date", "rep_key", "account_number", "cost_center",
                "revenue", "gross_profit", "fiscal_year", "fiscal_period",
            ]
        )
    out = df.copy()
    if "salesperson_desc" in out.columns:
        out["rep_key"] = out["salesperson_desc"].fillna("").astype(str).str.strip()
    elif "rep_key" not in out.columns:
        out["rep_key"] = ""
    for c in ("account_number", "cost_center"):
        if c in out.columns:
            out[c] = out[c].fillna("").astype(str).str.strip()
        else:
            out[c] = ""
    for c in ("revenue", "gross_profit"):
        out[c] = pd.to_numeric(out.get(c, 0), errors="coerce").fillna(0.0)
    if "invoice_date" not in out.columns:
        out["invoice_date"] = pd.NaT
    return out


def normalise_sample_product_pairs(
    mapping: dict[str, str] | None,
) -> dict[str, str]:
    """Return a ``{sample_cc: product_cc}`` dict regardless of which side
    the user typed in. Sample CCs always start with ``'1'``; product CCs
    always start with ``'0'``. If a pair has both keys starting the same
    way (or neither side is a sample), it is dropped."""
    out: dict[str, str] = {}
    for k, v in (mapping or {}).items():
        ks, vs = str(k or "").strip(), str(v or "").strip()
        if not ks or not vs:
            continue
        if ks.startswith("1") and vs.startswith("0"):
            out[ks] = vs
        elif vs.startswith("1") and ks.startswith("0"):
            out[vs] = ks
    return out


# ----------------------------------------------------------------- core
def compute_rep_scorecards(
    sales_df: pd.DataFrame,
    *,
    prior_df: pd.DataFrame | None = None,
    assignments_df: pd.DataFrame | None = None,
    displays_df: pd.DataFrame | None = None,
    samples_df: pd.DataFrame | None = None,
    core_displays_by_cc: dict[str, list[str]] | None = None,
    sample_to_product_cc: dict[str, str] | None = None,
    price_class_lookup: dict[str, str] | None = None,
    today: date | None = None,
) -> dict[str, RepScorecard]:
    """Compute one :class:`RepScorecard` per rep from already-loaded
    DataFrames. All inputs are optional except ``sales_df``.

    * ``sales_df`` / ``prior_df`` — output of
      :func:`app.data.loaders.load_blended_sales`. ``prior_df`` should cover
      the same span shifted back exactly one year.
    * ``assignments_df`` — output of
      :func:`app.data.loaders.load_rep_assignments` (gives total account
      count + closed-account flag).
    * ``displays_df`` — output of
      :func:`app.data.loaders.load_display_placements` (account x display).
    * ``samples_df`` — sales DataFrame restricted to sample CCs (``code_prefix='1'``).
    * ``core_displays_by_cc`` — :pyattr:`AppConfig.core_displays_by_cc`.
    """
    today = today or date.today()
    sales = _normalise_sales(sales_df)
    prior = _normalise_sales(prior_df) if prior_df is not None else None

    # ---- per-rep current-window aggregates ----
    if sales.empty:
        return {}

    # Account counts & active-account fraction (current window).
    acct_per_rep = (
        sales[sales["rep_key"] != ""]
        .groupby("rep_key")["account_number"].nunique().to_dict()
    )

    # Rep totals
    grp = sales[sales["rep_key"] != ""].groupby("rep_key", as_index=False).agg(
        revenue=("revenue", "sum"),
        gross_profit=("gross_profit", "sum"),
        invoice_lines=("revenue", "size"),
    )
    grp["gpp_pct"] = grp.apply(
        lambda r: (r["gross_profit"] / r["revenue"] * 100.0) if r["revenue"] else 0.0,
        axis=1,
    )
    rep_totals = {r["rep_key"]: r for r in grp.to_dict("records")}

    # Prior-year per-rep
    prior_totals: dict[str, float] = {}
    if prior is not None and not prior.empty:
        prior_g = (
            prior[prior["rep_key"] != ""]
            .groupby("rep_key", as_index=False)["revenue"].sum()
        )
        prior_totals = dict(zip(prior_g["rep_key"], prior_g["revenue"]))

    # Last 3 fiscal months vs prior 3 months & vs same 3 months last year
    last_3mo_start, last_3mo_end = _last_3_months_range(today)
    prior_3mo_start = last_3mo_start - (last_3mo_end - last_3mo_start) - timedelta(days=1)
    prior_3mo_end = last_3mo_start - timedelta(days=1)
    last_3mo_by_rep = _slice_revenue_by_rep(sales, last_3mo_start, last_3mo_end)
    prior_3mo_by_rep = _slice_revenue_by_rep(sales, prior_3mo_start, prior_3mo_end)
    yoy_3mo_by_rep: dict[str, float] = {}
    if prior is not None and not prior.empty:
        try:
            yoy_3mo_by_rep = _slice_revenue_by_rep(
                prior,
                last_3mo_start.replace(year=last_3mo_start.year - 1),
                last_3mo_end.replace(year=last_3mo_end.year - 1),
            )
        except ValueError:
            yoy_3mo_by_rep = {}

    # Total-account count from BILLSLMN (open accounts only — closed
    # accounts are surfaced as context elsewhere).
    total_accts: dict[str, int] = {}
    open_accts_by_rep: dict[str, set[str]] = {}
    account_info: dict[str, dict] = {}  # account_number -> {old, name}
    if assignments_df is not None and not assignments_df.empty:
        a = assignments_df.copy()
        a["salesman_name"] = a["salesman_name"].fillna("").astype(str).str.strip()
        a["account_number"] = a["account_number"].fillna("").astype(str).str.strip()
        if "old_account_number" in a.columns:
            a["old_account_number"] = a["old_account_number"].fillna("").astype(str).str.strip()
        if "account_name" in a.columns:
            a["account_name"] = (
                a["account_name"].fillna("").astype(str).str.lstrip("*").str.strip()
            )
        # Account info map (one row per account — first non-empty wins).
        for r in a.drop_duplicates(subset="account_number").itertuples(index=False):
            acct = getattr(r, "account_number", "") or ""
            if not acct:
                continue
            account_info[acct] = {
                "old": getattr(r, "old_account_number", "") or "",
                "name": getattr(r, "account_name", "") or "",
            }
        a = a[a["salesman_name"] != ""]
        if "is_closed" in a.columns:
            a = a[~a["is_closed"].astype(bool)]
        for rep, sub in a.groupby("salesman_name"):
            accts = {x for x in sub["account_number"].unique() if x}
            open_accts_by_rep[rep] = accts
            total_accts[rep] = len(accts)

    # Core-display coverage per rep. If the manager has not configured any
    # "core" displays for the cost centers in scope, we fall back to
    # "any display placement counts as coverage" so the metric reflects
    # reality instead of always reading 0.
    core_set: set[tuple[str, str]] = set()
    used_any_display_fallback = False
    if displays_df is not None and not displays_df.empty:
        d = displays_df.copy()
        d["account_number"] = d.get("account_number", "").astype(str).str.strip()
        d["display_code"] = d.get("display_code", "").astype(str).str.strip()
        flat_core = (
            {c for codes in (core_displays_by_cc or {}).values() for c in codes}
            if core_displays_by_cc else set()
        )
        if flat_core:
            d = d[d["display_code"].isin(flat_core)]
        else:
            used_any_display_fallback = True
        for r in d.itertuples(index=False):
            core_set.add((r.account_number, r.display_code))
    accounts_with_core: dict[str, set[str]] = {}
    for acct, _code in core_set:
        for rep, accts in open_accts_by_rep.items():
            if acct in accts:
                accounts_with_core.setdefault(rep, set()).add(acct)

    # Samples per rep. Sample order lines almost always have a blank
    # ``salesperson_desc`` (samples are pulled by inside-sales / customer
    # service, not the rep), so attribute by *account ownership* on the
    # mapped product CC instead of by ``rep_key``.
    samples_per_rep_lines: dict[str, int] = {}
    samples_per_rep_revenue: dict[str, float] = {}
    if samples_df is not None and not samples_df.empty:
        s = _normalise_sales(samples_df)
        sample_to_product = normalise_sample_product_pairs(sample_to_product_cc)
        # Build (account_number, cost_center) -> rep_key from assignments.
        rep_by_acct_cc: dict[tuple[str, str], str] = {}
        # And a fallback (account_number) -> rep_key (any product CC) for
        # samples whose CC has no explicit mapping.
        rep_by_acct_any: dict[str, str] = {}
        if assignments_df is not None and not assignments_df.empty:
            ax = assignments_df.copy()
            ax["salesman_name"] = ax["salesman_name"].fillna("").astype(str).str.strip()
            ax["account_number"] = ax["account_number"].fillna("").astype(str).str.strip()
            ax["cost_center"] = ax["cost_center"].fillna("").astype(str).str.strip()
            ax = ax[ax["salesman_name"] != ""]
            for r in ax.itertuples(index=False):
                acct = r.account_number
                cc = r.cost_center
                rep = r.salesman_name
                if acct and cc:
                    rep_by_acct_cc.setdefault((acct, cc), rep)
                if acct and acct not in rep_by_acct_any and cc.startswith("0"):
                    rep_by_acct_any[acct] = rep
        for r in s.itertuples(index=False):
            acct = getattr(r, "account_number", "") or ""
            sample_cc = getattr(r, "cost_center", "") or ""
            if not acct:
                continue
            product_cc = sample_to_product.get(sample_cc, "")
            rep = (
                rep_by_acct_cc.get((acct, product_cc), "") if product_cc else ""
            ) or rep_by_acct_any.get(acct, "")
            if not rep:
                continue
            samples_per_rep_lines[rep] = samples_per_rep_lines.get(rep, 0) + 1
            samples_per_rep_revenue[rep] = (
                samples_per_rep_revenue.get(rep, 0.0) + float(r.revenue or 0)
            )

    # Per-account YoY for top-mover lists (only need the rep's own accts).
    acct_curr = (
        sales[sales["rep_key"] != ""]
        .groupby(["rep_key", "account_number"], as_index=False)["revenue"].sum()
    )
    acct_prior = pd.DataFrame(columns=["rep_key", "account_number", "revenue"])
    if prior is not None and not prior.empty:
        acct_prior = (
            prior[prior["rep_key"] != ""]
            .groupby(["rep_key", "account_number"], as_index=False)["revenue"].sum()
        )
    acct_join = acct_curr.merge(
        acct_prior, on=["rep_key", "account_number"], how="outer",
        suffixes=("_curr", "_prior"),
    ).fillna(0.0)

    # Peer-set YoY average — reps with enough revenue & accounts to count.
    # Outliers (|YoY| > OUTLIER_YOY_PCT_THRESHOLD) are excluded so a single
    # rep with a huge transferred-account swing can't drag the peer average
    # away from reality.
    peer_yoys: list[float] = []
    rep_yoys: dict[str, float] = {}
    rep_outliers: set[str] = set()
    for rep_key, totals in rep_totals.items():
        rev = float(totals["revenue"])
        prior_rev = float(prior_totals.get(rep_key, 0.0))
        accts = int(acct_per_rep.get(rep_key, 0))
        y = _yoy_pct(rev, prior_rev)
        if y is None or rev < MIN_REVENUE_FOR_YOY or accts < MIN_ACCOUNTS_FOR_PEER_COMPARE:
            continue
        if abs(y) > OUTLIER_YOY_PCT_THRESHOLD:
            rep_outliers.add(rep_key)
            rep_yoys[rep_key] = y  # still recorded for the rep, just not in peer avg
            continue
        peer_yoys.append(y)
        rep_yoys[rep_key] = y
    peer_avg_yoy = sum(peer_yoys) / len(peer_yoys) if peer_yoys else None

    # Rankings within peer set
    rank_rev_order = sorted(rep_totals.items(), key=lambda kv: -float(kv[1]["revenue"]))
    rank_rev = {kv[0]: i + 1 for i, kv in enumerate(rank_rev_order)}
    rank_yoy_order = sorted(rep_yoys.items(), key=lambda kv: -kv[1])
    rank_yoy = {kv[0]: i + 1 for i, kv in enumerate(rank_yoy_order)}

    out: dict[str, RepScorecard] = {}
    for rep_key, totals in rep_totals.items():
        rev = float(totals["revenue"])
        prior_rev = float(prior_totals.get(rep_key, 0.0))
        accts_active = int(acct_per_rep.get(rep_key, 0))
        accts_total = int(total_accts.get(rep_key, accts_active))
        accts_with_core = len(accounts_with_core.get(rep_key, set()))
        sample_lines = int(samples_per_rep_lines.get(rep_key, 0))
        sample_rev = float(samples_per_rep_revenue.get(rep_key, 0.0))

        l3 = float(last_3mo_by_rep.get(rep_key, 0.0))
        p3 = float(prior_3mo_by_rep.get(rep_key, 0.0))
        y3 = float(yoy_3mo_by_rep.get(rep_key, 0.0))

        # Top movers — current rep only
        rep_acct_join = acct_join[acct_join["rep_key"] == rep_key].copy()
        rep_acct_join["delta"] = rep_acct_join["revenue_curr"] - rep_acct_join["revenue_prior"]
        rep_acct_join["pct"] = rep_acct_join.apply(
            lambda r: _yoy_pct(r["revenue_curr"], r["revenue_prior"]),
            axis=1,
        )
        growing = (
            rep_acct_join[rep_acct_join["delta"] > 0]
            .sort_values("delta", ascending=False).head(TOP_N_ACCOUNTS)
        )
        declining = (
            rep_acct_join[rep_acct_join["delta"] < 0]
            .sort_values("delta", ascending=True).head(TOP_N_ACCOUNTS)
        )
        stale = (
            rep_acct_join[(rep_acct_join["revenue_curr"] == 0) & (rep_acct_join["revenue_prior"] > 0)]
            .sort_values("revenue_prior", ascending=False).head(TOP_N_ACCOUNTS)
        )
        new_a = (
            rep_acct_join[(rep_acct_join["revenue_prior"] == 0) & (rep_acct_join["revenue_curr"] > 0)]
            .sort_values("revenue_curr", ascending=False).head(TOP_N_ACCOUNTS)
        )

        notes: list[str] = []
        if accts_active < MIN_ACCOUNTS_FOR_PEER_COMPARE:
            notes.append(
                f"Only {accts_active} active account(s) — peer comparisons "
                "are skipped (sample too small)."
            )
        if rev < MIN_REVENUE_FOR_YOY:
            notes.append("Revenue below YoY-comparison threshold; YoY hidden.")
        if rep_key in rep_outliers:
            notes.append(
                "YoY swing is outside the normal range (likely an "
                "account-territory transfer) — excluded from peer average. "
                "Coaching email will focus on absolute revenue, GP%, "
                "3-month momentum, and account mix."
            )
        if used_any_display_fallback:
            notes.append(
                "No core displays configured for these cost centers — "
                "coverage shown is *any* display on file."
            )

        # Top price classes (by revenue) for this rep — used to surface
        # product-type patterns in the coaching email and AI prompt.
        pc_top: list[dict] = []
        if "price_class" in sales.columns:
            rep_pc = sales[
                (sales["rep_key"] == rep_key)
                & sales["price_class"].notna()
                & (sales["price_class"].astype(str).str.strip() != "")
            ]
            if not rep_pc.empty:
                pc_grp = rep_pc.groupby("price_class", as_index=False).agg(
                    revenue=("revenue", "sum"),
                    gross_profit=("gross_profit", "sum"),
                    lines=("revenue", "size"),
                )
                pc_grp = pc_grp.sort_values("revenue", ascending=False).head(8)
                for r in pc_grp.to_dict("records"):
                    pc = str(r.get("price_class") or "").strip()
                    # Use "" as the default so the caller's `desc or code` fallback works.
                    desc = (price_class_lookup or {}).get(pc) or ""
                    rev_pc = float(r.get("revenue") or 0.0)
                    gp_pc = float(r.get("gross_profit") or 0.0)
                    gp_pct_pc = (gp_pc / rev_pc * 100.0) if rev_pc else 0.0
                    pc_top.append({
                        "price_class": pc,
                        "desc": desc,
                        "revenue": rev_pc,
                        "gp_pct": round(gp_pct_pc, 1),
                        "lines": int(r.get("lines") or 0),
                    })

        out[rep_key] = RepScorecard(
            rep_key=rep_key,
            rep_name=rep_key,  # rep_key is the human-readable salesperson_desc
            revenue=rev,
            prior_revenue=prior_rev,
            yoy_pct=rep_yoys.get(rep_key) if rep_key in rep_yoys else _yoy_pct(rev, prior_rev),
            peer_avg_yoy_pct=peer_avg_yoy,
            vs_peers_pct=(
                (rep_yoys[rep_key] - peer_avg_yoy)
                if (peer_avg_yoy is not None and rep_key in rep_yoys)
                else None
            ),
            gross_profit=float(totals["gross_profit"]),
            gpp_pct=float(totals["gpp_pct"]),
            invoice_lines=int(totals["invoice_lines"]),
            total_accounts=accts_total,
            active_accounts=accts_active,
            active_account_pct=(accts_active / accts_total * 100.0) if accts_total else 0.0,
            accounts_with_core_displays=accts_with_core,
            core_display_coverage_pct=(
                (accts_with_core / accts_total * 100.0) if accts_total else 0.0
            ),
            sample_lines=sample_lines,
            sample_revenue=sample_rev,
            samples_per_account=(sample_lines / accts_total) if accts_total else 0.0,
            last_3mo_revenue=l3,
            prior_3mo_revenue=p3,
            last_3mo_vs_prior_3mo_pct=_yoy_pct(l3, p3),
            last_3mo_yoy_pct=_yoy_pct(l3, y3),
            top_growing_accounts=_records_for_email(growing, account_info=account_info),
            top_declining_accounts=_records_for_email(declining, account_info=account_info),
            stale_accounts=_records_for_email(stale, prior_only=True, account_info=account_info),
            new_accounts=_records_for_email(new_a, account_info=account_info),
            rank_revenue=rank_rev.get(rep_key),
            rank_yoy=rank_yoy.get(rep_key),
            peer_count=len(peer_yoys),
            is_yoy_outlier=(rep_key in rep_outliers),
            price_class_top=pc_top,
            notes=notes,
        )
    return out


def _records_for_email(
    df: pd.DataFrame,
    *,
    prior_only: bool = False,
    account_info: dict[str, dict] | None = None,
) -> list[dict]:
    out = []
    info = account_info or {}
    for r in df.to_dict("records"):
        acct = r.get("account_number", "") or ""
        meta = info.get(acct, {})
        row = {
            "account": acct,
            "old_account": meta.get("old", ""),
            "account_name": meta.get("name", ""),
            "current": float(r.get("revenue_curr", 0.0)),
            "prior": float(r.get("revenue_prior", 0.0)),
            "delta": float(r.get("delta", 0.0)),
            "pct": r.get("pct"),
        }
        if prior_only:
            row.pop("delta", None)
            row.pop("pct", None)
            row.pop("current", None)
        out.append(row)
    return out


def format_account_label(rec: dict, *, style: str = "short") -> str:
    """Render a top-mover account record for emails / UI.

    ``style='short'``  -> ``"50285 (#1234)"`` or ``"50285 (ABC FLOORING)"``
    ``style='long'``   -> ``"50285 · ABC FLOORING (#1234)"``
    Old-system account number (BBANK2) is shown in parentheses with a ``#``
    prefix because reps know their accounts by the legacy number.
    """
    acct = str(rec.get("account", "") or "").strip()
    old = str(rec.get("old_account", "") or "").strip()
    name = str(rec.get("account_name", "") or "").strip()
    if style == "long":
        bits = [acct]
        if name:
            bits.append("· " + name)
        if old and old != acct:
            bits.append(f"(#{old})")
        return " ".join(bits).strip()
    # short
    paren_bits: list[str] = []
    if old and old != acct:
        paren_bits.append(f"#{old}")
    if name:
        paren_bits.append(name)
    if paren_bits:
        return f"{acct} ({' · '.join(paren_bits)})"
    return acct


def _slice_revenue_by_rep(
    sales: pd.DataFrame, start: date, end: date
) -> dict[str, float]:
    if sales.empty or "invoice_date" not in sales.columns:
        return {}
    s = sales.copy()
    s["invoice_date"] = pd.to_datetime(s["invoice_date"], errors="coerce")
    mask = (s["invoice_date"] >= pd.Timestamp(start)) & (s["invoice_date"] <= pd.Timestamp(end))
    sub = s[mask & (s["rep_key"] != "")]
    if sub.empty:
        return {}
    return sub.groupby("rep_key")["revenue"].sum().to_dict()


def _last_3_months_range(today: date) -> tuple[date, date]:
    """Approx last 90 days ending yesterday — used as a coarser
    'momentum' window than the fiscal calendar buckets."""
    end = today - timedelta(days=1)
    start = end - timedelta(days=89)
    return start, end


# ----------------------------------------------------------------- weekly windows
def current_week_range(today: date | None = None, *, week_start: int = 6) -> tuple[date, date]:
    """The Sunday→Saturday window containing ``today`` (default).

    ``week_start=6`` means weeks start on Sunday; pass ``0`` for Monday.
    """
    today = today or date.today()
    delta = (today.weekday() - week_start) % 7
    start = today - timedelta(days=delta)
    end = start + timedelta(days=6)
    return start, end


def previous_week_range(today: date | None = None, *, week_start: int = 6) -> tuple[date, date]:
    s, _ = current_week_range(today, week_start=week_start)
    return s - timedelta(days=7), s - timedelta(days=1)


def revenue_in_window(
    sales: pd.DataFrame, start: date, end: date, *, by: str = "rep"
) -> dict[str, float]:
    """Sum revenue between ``start``..``end`` keyed by ``rep`` or
    ``account``. Empty dict if no rows match."""
    s = _normalise_sales(sales)
    if s.empty:
        return {}
    s["invoice_date"] = pd.to_datetime(s["invoice_date"], errors="coerce")
    mask = (s["invoice_date"] >= pd.Timestamp(start)) & (s["invoice_date"] <= pd.Timestamp(end))
    sub = s[mask]
    if sub.empty:
        return {}
    key = "rep_key" if by == "rep" else "account_number"
    return sub.groupby(key)["revenue"].sum().to_dict()


# ----------------------------------------------------------------- period overview
def compute_period_overview(
    label: str,
    start: date,
    end: date,
    sales_df: pd.DataFrame,
    prior_df: pd.DataFrame | None = None,
) -> PeriodOverview:
    """Roll ``sales_df`` (already filtered to ``start..end``) into a
    high-level overview suitable for "first email after end of period"
    summaries (monthly, quarterly, yearly).
    """
    sales = _normalise_sales(sales_df)
    prior = _normalise_sales(prior_df) if prior_df is not None else None

    rev = float(sales["revenue"].sum()) if not sales.empty else 0.0
    gp = float(sales["gross_profit"].sum()) if not sales.empty else 0.0
    prior_rev = float(prior["revenue"].sum()) if prior is not None and not prior.empty else 0.0

    top_reps_df = (
        sales[sales["rep_key"] != ""]
        .groupby("rep_key", as_index=False)["revenue"].sum()
        .sort_values("revenue", ascending=False)
    )
    top_reps = [
        {"rep": r["rep_key"], "revenue": float(r["revenue"])}
        for r in top_reps_df.head(10).to_dict("records")
    ]
    bottom_reps = [
        {"rep": r["rep_key"], "revenue": float(r["revenue"])}
        for r in top_reps_df.tail(5).to_dict("records")
    ]

    # CC growth/decline vs prior
    cc_curr = sales.groupby("cost_center", as_index=False)["revenue"].sum()
    cc_prior = (
        prior.groupby("cost_center", as_index=False)["revenue"].sum()
        if prior is not None and not prior.empty
        else pd.DataFrame(columns=["cost_center", "revenue"])
    )
    cc_join = cc_curr.merge(cc_prior, on="cost_center", how="outer",
                            suffixes=("_curr", "_prior")).fillna(0.0)
    cc_join["delta"] = cc_join["revenue_curr"] - cc_join["revenue_prior"]
    top_growing = [
        {"cost_center": r["cost_center"], "delta": float(r["delta"]),
         "current": float(r["revenue_curr"]), "prior": float(r["revenue_prior"])}
        for r in cc_join.sort_values("delta", ascending=False).head(5).to_dict("records")
        if r["delta"] > 0
    ]
    top_declining = [
        {"cost_center": r["cost_center"], "delta": float(r["delta"]),
         "current": float(r["revenue_curr"]), "prior": float(r["revenue_prior"])}
        for r in cc_join.sort_values("delta", ascending=True).head(5).to_dict("records")
        if r["delta"] < 0
    ]

    return PeriodOverview(
        label=label,
        start=start,
        end=end,
        revenue=rev,
        prior_revenue=prior_rev,
        yoy_pct=_yoy_pct(rev, prior_rev),
        gross_profit=gp,
        gpp_pct=(gp / rev * 100.0) if rev else 0.0,
        invoice_lines=int(len(sales)),
        active_reps=int(sales[sales["rep_key"] != ""]["rep_key"].nunique()),
        active_accounts=int(sales[sales["account_number"] != ""]["account_number"].nunique()),
        top_reps=top_reps,
        bottom_reps=bottom_reps,
        top_growing_ccs=top_growing,
        top_declining_ccs=top_declining,
    )


# ----------------------------------------------------------------- aggregation snapshots for AI
def aggregate_for_ai(sales_df: pd.DataFrame) -> dict[str, list[dict]]:
    """Compact pre-aggregated tables the AI can rely on without scanning
    every CSV row. Returns ``{by_rep, by_cc, by_account, by_period}``.

    Each list is sorted by revenue desc and capped to keep token usage in
    check; the AI is told the totals so it can answer "top N" questions
    deterministically.
    """
    sales = _normalise_sales(sales_df)
    if sales.empty:
        return {"by_rep": [], "by_cc": [], "by_account": [], "by_period": []}

    by_rep = (
        sales.groupby("rep_key", as_index=False)
             .agg(revenue=("revenue", "sum"),
                  gross_profit=("gross_profit", "sum"),
                  lines=("revenue", "size"),
                  accounts=("account_number", "nunique"))
             .sort_values("revenue", ascending=False)
    )
    by_cc = (
        sales.groupby("cost_center", as_index=False)
             .agg(revenue=("revenue", "sum"),
                  gross_profit=("gross_profit", "sum"),
                  lines=("revenue", "size"),
                  accounts=("account_number", "nunique"))
             .sort_values("revenue", ascending=False)
    )
    by_account = (
        sales.groupby("account_number", as_index=False)
             .agg(revenue=("revenue", "sum"),
                  gross_profit=("gross_profit", "sum"),
                  lines=("revenue", "size"))
             .sort_values("revenue", ascending=False)
             .head(200)  # 200 accounts is enough for narrative, keeps tokens sane
    )
    if "fiscal_year" in sales.columns and "fiscal_period" in sales.columns:
        by_period = (
            sales.groupby(["fiscal_year", "fiscal_period", "fiscal_period_name"],
                          as_index=False)
                 .agg(revenue=("revenue", "sum"),
                      lines=("revenue", "size"))
                 .sort_values(["fiscal_year", "fiscal_period"])
        )
    else:
        by_period = pd.DataFrame()

    def _records(df: pd.DataFrame) -> list[dict]:
        return [
            {k: (float(v) if isinstance(v, (int, float)) and k in ("revenue", "gross_profit") else
                 (int(v) if isinstance(v, (int, float)) and k in ("lines", "accounts", "fiscal_year", "fiscal_period") else v))
             for k, v in r.items()}
            for r in df.to_dict("records")
        ]
    return {
        "by_rep": _records(by_rep),
        "by_cc": _records(by_cc),
        "by_account": _records(by_account),
        "by_period": _records(by_period) if not by_period.empty else [],
    }
