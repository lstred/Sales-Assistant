"""Tests for manager analytics outlier handling and account labels."""

import pandas as pd

from app.services.manager_analytics import (
    compute_rep_scorecards,
    format_account_label,
    normalise_sample_product_pairs,
)


def test_outlier_excluded_from_peer_avg() -> None:
    rows = []
    for rep, mult in [("A", 1.0), ("B", 1.05), ("C", 0.95), ("OUT", 11.0)]:
        for n in range(6):
            rows.append(
                {
                    "salesperson_desc": rep,
                    "account_number": f"{rep}{n}",
                    "cost_center": "010",
                    "revenue": 200.0 * mult,
                    "gross_profit": 40.0,
                    "invoice_date": pd.Timestamp("2026-04-15"),
                }
            )
    cur = pd.DataFrame(rows)
    prior_rows = []
    for rep in ("A", "B", "C", "OUT"):
        for n in range(6):
            base = 200.0 if rep != "OUT" else (200.0 / 11.0)
            prior_rows.append(
                {
                    "salesperson_desc": rep,
                    "account_number": f"{rep}{n}",
                    "cost_center": "010",
                    "revenue": base,
                    "gross_profit": 40.0,
                    "invoice_date": pd.Timestamp("2025-04-15"),
                }
            )
    prior = pd.DataFrame(prior_rows)
    cards = compute_rep_scorecards(cur, prior_df=prior)
    assert cards["OUT"].is_yoy_outlier is True
    assert cards["A"].is_yoy_outlier is False
    peer = cards["A"].peer_avg_yoy_pct
    assert peer is not None and abs(peer) < 50.0, (
        f"peer_avg leaked outlier: {peer}"
    )


def test_account_label_short_and_long() -> None:
    rec = {"account": "50285", "old_account": "1234", "account_name": "ABC"}
    assert format_account_label(rec) == "50285 (#1234 \u00b7 ABC)"
    assert (
        format_account_label(
            {"account": "50285", "old_account": "", "account_name": ""}
        )
        == "50285"
    )
    assert format_account_label(rec, style="long") == "50285 \u00b7 ABC (#1234)"

def test_normalise_sample_product_pairs_either_direction() -> None:
    # User typed product->sample (the wrong way around in the UI)
    assert normalise_sample_product_pairs({'010': '141'}) == {'141': '010'}
    # User typed sample->product (the canonical direction)
    assert normalise_sample_product_pairs({'141': '010'}) == {'141': '010'}
    # Mixed dict, only valid pairs survive
    assert normalise_sample_product_pairs(
        {'010': '141', '141': '010', '011': '010', '': 'x'}
    ) == {'141': '010'}


def test_samples_attributed_via_account_ownership() -> None:
    import pandas as pd
    # Product sales: rep A owns acct 100 on CC 010
    sales = pd.DataFrame([{
        'salesperson_desc': 'A', 'account_number': '100', 'cost_center': '010',
        'revenue': 1000.0, 'gross_profit': 200.0,
        'invoice_date': pd.Timestamp('2026-04-15'),
    }])
    # Sample line on account 100 / CC 141 with BLANK salesperson_desc
    samples = pd.DataFrame([{
        'salesperson_desc': '', 'account_number': '100', 'cost_center': '141',
        'revenue': 0.0, 'gross_profit': 0.0,
        'invoice_date': pd.Timestamp('2026-04-15'),
    }] * 5)
    assignments = pd.DataFrame([{
        'salesman_name': 'A', 'salesman_number': '1', 'cost_center': '010',
        'account_number': '100', 'old_account_number': '99',
        'account_name': 'X', 'is_closed': False,
    }])
    cards = compute_rep_scorecards(
        sales, assignments_df=assignments, samples_df=samples,
        sample_to_product_cc={'141': '010'},
    )
    assert cards['A'].sample_lines == 5, cards['A'].sample_lines


# ─────────────────────── Budget service tests ────────────────────────────────

def test_parse_rep_cc_upload_from_csv(tmp_path) -> None:
    """parse_rep_cc_upload reads a CSV correctly and zero-pads CC codes."""
    from app.services.budget_service import parse_rep_cc_upload

    csv_path = tmp_path / "overrides.csv"
    csv_path.write_text(
        "rep_number,cost_center,growth_pct\n"
        "42,10,10.0\n"    # CC "10" should be normalised to "010"
        "17,10,-5.5\n"    # same
        "42,20,0.0\n"     # "20" → "020"
        "5,010,3.0\n"     # already 3-char, kept; rep "5" stripped of leading zeros
    )
    overrides, errors = parse_rep_cc_upload(str(csv_path))
    assert errors == []
    # CC codes are zero-padded to 3 digits
    assert overrides[("42", "010")] == 10.0
    assert overrides[("17", "010")] == -5.5
    assert overrides[("42", "020")] == 0.0
    assert overrides[("5", "010")] == 3.0


def test_parse_rep_cc_upload_skips_bad_rows(tmp_path) -> None:
    """Bad rows emit errors but valid rows are still returned. CC zero-padded."""
    from app.services.budget_service import parse_rep_cc_upload

    csv_path = tmp_path / "bad.csv"
    csv_path.write_text(
        "rep_number,cost_center,growth_pct\n"
        "42,10,10.0\n"       # valid — CC "10" → "010"
        ",,\n"               # blank rep + cc → error
        "17,20,notanumber\n" # bad growth_pct → error
    )
    overrides, errors = parse_rep_cc_upload(str(csv_path))
    assert ("42", "010") in overrides
    assert len(errors) == 2   # blank row + bad number


def test_budget_rep_cc_override_takes_priority() -> None:
    """When rep_cc_growth_pct is provided, each rep uses their own rate."""
    import pandas as pd
    from app.services.budget_service import compute_budget_by_rep

    prior_df = pd.DataFrame([
        {"account_number": "A1", "cost_center": "010", "revenue": 10_000.0, "gross_profit": 2_000.0},
        {"account_number": "A2", "cost_center": "010", "revenue": 5_000.0, "gross_profit": 1_000.0},
    ])
    assignments_df = pd.DataFrame([
        {"salesman_number": "1", "salesman_name": "Alpha", "cost_center": "010", "account_number": "A1"},
        {"salesman_number": "2", "salesman_name": "Beta",  "cost_center": "010", "account_number": "A2"},
    ])
    # Alpha gets +20%, Beta gets -10% — different from the CC default (+5%)
    cc_growth = {"010": 5.0}
    rep_cc = {("1", "010"): 20.0, ("2", "010"): -10.0}
    seasonality = [100.0 / 12] * 12

    rows = compute_budget_by_rep(
        prior_df, assignments_df, cc_growth, seasonality, {}, rep_cc_growth_pct=rep_cc
    )
    by_rep = {r.rep_number: r for r in rows}

    assert abs(by_rep["1"].budget_full_year - 12_000.0) < 0.01  # 10k * 1.20
    assert abs(by_rep["2"].budget_full_year - 4_500.0) < 0.01   # 5k * 0.90
    assert abs(by_rep["1"].growth_pct - 20.0) < 0.001
    assert abs(by_rep["2"].growth_pct - (-10.0)) < 0.001


def test_budget_cc_level_fallback_when_no_override() -> None:
    """Reps with no rep_cc entry fall back to the CC-level growth %."""
    import pandas as pd
    from app.services.budget_service import compute_budget_by_rep

    prior_df = pd.DataFrame([
        {"account_number": "A1", "cost_center": "010", "revenue": 8_000.0, "gross_profit": 0.0},
    ])
    assignments_df = pd.DataFrame([
        {"salesman_number": "5", "salesman_name": "Gamma", "cost_center": "010", "account_number": "A1"},
    ])
    cc_growth = {"010": 10.0}
    seasonality = [100.0 / 12] * 12

    rows_no_override = compute_budget_by_rep(
        prior_df, assignments_df, cc_growth, seasonality, {}
    )
    rows_with_empty = compute_budget_by_rep(
        prior_df, assignments_df, cc_growth, seasonality, {}, rep_cc_growth_pct={}
    )
    for rows in (rows_no_override, rows_with_empty):
        assert len(rows) == 1
        assert abs(rows[0].budget_full_year - 8_800.0) < 0.01   # 8k * 1.10
