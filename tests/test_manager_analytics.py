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
