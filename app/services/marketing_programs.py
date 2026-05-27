"""Marketing programs helpers — shared formatting for AI surfaces.

Marketing programs are sourced from ``dbo.CLASSES`` (``CLCAT='MP'``) for the
catalog and ``dbo.BILL_CD`` (``BCCAT='MP'``) for per-account enrollments.
They are categorised locally in the app (e.g. "CCA Buying Group", "NRF
Rebate Program") by :attr:`AppConfig.marketing_program_categories` and
:attr:`AppConfig.marketing_program_category_by_code`, and individual
programs can be starred to flag them as high-priority for AI analysis.

This module produces compact plain-text blocks that drop into any AI prompt
(weekly email, conversations reply, Ask the AI) so the model can look for
correlations between program enrollment and account/rep performance.
"""

from __future__ import annotations

from typing import Iterable

import pandas as pd


UNCATEGORIZED = "Uncategorized"


def normalise_placements(placements_df: pd.DataFrame | None) -> pd.DataFrame:
    """Return a copy with ``account_number``/``program_code`` stripped to str.

    Idempotent and safe to call on an empty DataFrame.
    """
    if placements_df is None or placements_df.empty:
        return pd.DataFrame(columns=["account_number", "program_code", "program_desc"])
    df = placements_df.copy()
    df["account_number"] = df["account_number"].fillna("").astype(str).str.strip()
    df["program_code"] = df["program_code"].fillna("").astype(str).str.strip()
    if "program_desc" in df.columns:
        df["program_desc"] = df["program_desc"].fillna("").astype(str).str.strip()
    return df[(df["account_number"] != "") & (df["program_code"] != "")]


def build_program_directory(
    program_types_df: pd.DataFrame | None,
    category_by_code: dict[str, str] | None,
    starred: Iterable[str] | None,
) -> dict[str, dict[str, str | bool]]:
    """Return ``{program_code: {desc, category, starred}}``.

    A program absent from ``category_by_code`` is bucketed as
    :data:`UNCATEGORIZED`. A program absent from ``program_types_df`` (e.g.
    used in a placement row but no longer in CLASSES) still appears with
    ``desc=''``.
    """
    starred_set = {str(c).strip() for c in (starred or []) if str(c).strip()}
    cat_map = {str(k).strip(): str(v).strip() for k, v in (category_by_code or {}).items()}

    out: dict[str, dict[str, str | bool]] = {}
    if program_types_df is not None and not program_types_df.empty:
        for _, row in program_types_df.iterrows():
            code = str(row.get("program_code", "")).strip()
            if not code:
                continue
            out[code] = {
                "desc": str(row.get("program_desc", "") or "").strip(),
                "category": cat_map.get(code, UNCATEGORIZED),
                "starred": code in starred_set,
            }
    return out


def summarise_for_ai(
    placements_df: pd.DataFrame | None,
    program_types_df: pd.DataFrame | None,
    category_by_code: dict[str, str] | None,
    starred: Iterable[str] | None,
    *,
    account_filter: Iterable[str] | None = None,
    max_lines: int = 30,
) -> str:
    """Return a compact plain-text MP summary for any AI prompt.

    The block contains:
      • CATEGORY ROLL-UP — total enrolled accounts per high-level category.
      • STARRED PROGRAMS — per-program enrolled account count for programs
        the manager flagged as important.
      • TOP PROGRAMS — every other program sorted by enrolled account count
        (capped at ``max_lines``).

    When ``account_filter`` is provided, all counts are scoped to that set
    of account numbers — e.g. a single rep's territory.

    Returns ``""`` when nothing meaningful can be reported (avoids polluting
    prompts when MP data is not loaded yet).
    """
    placements = normalise_placements(placements_df)
    if placements.empty:
        return ""

    if account_filter is not None:
        accounts = {str(a).strip() for a in account_filter if str(a).strip()}
        if not accounts:
            return ""
        placements = placements[placements["account_number"].isin(accounts)]
        if placements.empty:
            return ""

    directory = build_program_directory(program_types_df, category_by_code, starred)

    # Tag each placement with category + starred status from the directory.
    def _cat(code: str) -> str:
        d = directory.get(code)
        return d["category"] if d else UNCATEGORIZED  # type: ignore[return-value]

    def _starred(code: str) -> bool:
        d = directory.get(code)
        return bool(d["starred"]) if d else False

    def _desc(code: str) -> str:
        d = directory.get(code)
        return str(d["desc"]) if d and d.get("desc") else ""

    placements = placements.assign(
        _category=placements["program_code"].map(_cat),
        _starred=placements["program_code"].map(_starred),
    )

    # Category roll-up: unique accounts per category.
    cat_rows = (
        placements.groupby("_category")["account_number"]
        .nunique()
        .sort_values(ascending=False)
    )
    cat_lines = [
        f"  - {cat}: {n} enrolled account(s)" for cat, n in cat_rows.items()
    ]

    # Starred programs — every starred program gets its own line.
    starred_codes = sorted({c for c in placements["program_code"].unique() if _starred(c)})
    starred_lines: list[str] = []
    for code in starred_codes:
        n = placements[placements["program_code"] == code]["account_number"].nunique()
        desc = _desc(code) or "(no description)"
        cat = _cat(code)
        starred_lines.append(f"  - [{cat}] {code} — {desc}: {n} enrolled account(s)")

    # Top programs (not starred) — capped.
    other = placements[~placements["_starred"]]
    top_rows = (
        other.groupby("program_code")["account_number"]
        .nunique()
        .sort_values(ascending=False)
        .head(max_lines)
    )
    top_lines: list[str] = []
    for code, n in top_rows.items():
        desc = _desc(code) or "(no description)"
        cat = _cat(code)
        top_lines.append(f"  - [{cat}] {code} — {desc}: {n} enrolled account(s)")

    parts: list[str] = ["MARKETING PROGRAMS (enrollment from dbo.BILL_CD where BCCAT='MP'):"]
    parts.append("CATEGORY ROLL-UP:")
    parts.extend(cat_lines or ["  (no enrollments in scope)"])
    if starred_lines:
        parts.append("STARRED PROGRAMS (flagged as important by manager):")
        parts.extend(starred_lines)
    if top_lines:
        parts.append(f"TOP PROGRAMS BY ENROLLED ACCOUNTS (up to {max_lines}):")
        parts.extend(top_lines)
    return "\n".join(parts) + "\n"


def per_account_program_lines(
    placements_df: pd.DataFrame | None,
    program_types_df: pd.DataFrame | None,
    category_by_code: dict[str, str] | None,
    starred: Iterable[str] | None,
    account_numbers: Iterable[str],
    *,
    account_labels: dict[str, str] | None = None,
    only_starred: bool = False,
) -> str:
    """Return one line per account listing the programs that account is in.

    Format: ``  - 12345 · ABC FLOORING: [CCA Buying Group] CODE-A; [NRF] CODE-B``

    When ``only_starred`` is True, accounts without at least one starred
    program enrollment are omitted entirely (keeps prompts focused on the
    signals the manager has explicitly flagged).
    """
    placements = normalise_placements(placements_df)
    if placements.empty:
        return ""

    target_accts = [str(a).strip() for a in account_numbers if str(a).strip()]
    if not target_accts:
        return ""
    placements = placements[placements["account_number"].isin(target_accts)]
    if placements.empty:
        return ""

    directory = build_program_directory(program_types_df, category_by_code, starred)
    labels = {str(k).strip(): str(v).strip() for k, v in (account_labels or {}).items()}

    out: list[str] = []
    for acct, sub in placements.groupby("account_number"):
        progs: list[str] = []
        any_starred = False
        for code in sorted(sub["program_code"].unique()):
            d = directory.get(code) or {"category": UNCATEGORIZED, "starred": False, "desc": ""}
            cat = d["category"]
            star = "*" if d.get("starred") else ""
            if d.get("starred"):
                any_starred = True
            progs.append(f"[{cat}] {code}{star}")
        if only_starred and not any_starred:
            continue
        label = labels.get(acct, "")
        head = f"{acct} · {label}".rstrip(" ·") if label else acct
        out.append(f"  - {head}: {'; '.join(progs)}")
    if not out:
        return ""
    return "\n".join(out) + "\n"


def category_to_accounts_lines(
    placements_df: pd.DataFrame | None,
    program_types_df: pd.DataFrame | None,
    category_by_code: dict[str, str] | None,
    starred: Iterable[str] | None,
    *,
    account_filter: Iterable[str] | None = None,
    account_labels: dict[str, str] | None = None,
    max_per_category: int = 80,
) -> str:
    """Return an authoritative ``category -> accounts`` listing for AI prompts.

    Unlike :func:`summarise_for_ai` (which emits aggregate counts) this lists the
    actual enrolled accounts under each marketing category so a model asked
    "which CCA accounts ..." cannot hallucinate the membership. UNCATEGORIZED
    is intentionally skipped to keep prompts focused.
    """
    cat_map, _ = account_program_maps(
        placements_df, program_types_df, category_by_code, starred,
        only_starred=False,
    )
    if not cat_map:
        return ""
    scope: set[str] | None = None
    if account_filter is not None:
        scope = {str(a).strip() for a in account_filter if str(a).strip()}
    labels = account_labels or {}

    by_cat: dict[str, list[str]] = {}
    for acct, cats in cat_map.items():
        if scope is not None and acct not in scope:
            continue
        for cat in cats:
            if cat == UNCATEGORIZED:
                continue
            by_cat.setdefault(cat, []).append(acct)

    if not by_cat:
        return ""

    out: list[str] = [
        "ACCOUNTS BY MARKETING CATEGORY (AUTHORITATIVE \u2014 for any question "
        "naming a marketing category, use ONLY these accounts as ground truth; "
        "never include an account outside its category heading):"
    ]
    for cat in sorted(by_cat.keys()):
        accts = sorted(by_cat[cat])
        out.append(f"\n[{cat}] \u2014 {len(accts)} enrolled account(s):")
        for acct in accts[:max_per_category]:
            lbl = labels.get(acct)
            out.append(f"  - {lbl} [{acct}]" if lbl else f"  - [{acct}]")
        if len(accts) > max_per_category:
            out.append(f"  - \u2026 and {len(accts) - max_per_category} more (truncated for prompt size)")
    return "\n".join(out) + "\n"


def account_program_maps(
    placements_df: pd.DataFrame | None,
    program_types_df: pd.DataFrame | None,
    category_by_code: dict[str, str] | None,
    starred: Iterable[str] | None,
    *,
    only_starred: bool = False,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return ``(acct -> set[category], acct -> set[program_code])``.

    When ``only_starred=True`` only starred programs (and the categories they
    belong to) are included — used when the AI needs an authoritative answer
    to "which accounts are in <starred category>" questions.
    """
    cat_map: dict[str, set[str]] = {}
    code_map: dict[str, set[str]] = {}
    placements = normalise_placements(placements_df)
    if placements.empty:
        return cat_map, code_map
    directory = build_program_directory(program_types_df, category_by_code, starred)
    for _, row in placements.iterrows():
        code = str(row["program_code"]).strip()
        acct = str(row["account_number"]).strip()
        if not code or not acct:
            continue
        d = directory.get(code) or {"category": UNCATEGORIZED, "starred": False}
        if only_starred and not d.get("starred"):
            continue
        cat = str(d["category"])
        cat_map.setdefault(acct, set()).add(cat)
        code_map.setdefault(acct, set()).add(code)
    return cat_map, code_map
