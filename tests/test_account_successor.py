"""Closed-account -> open-successor merge tests."""
from __future__ import annotations

import pandas as pd

from app.data.loaders import _normalise_address, build_account_successor_map


def _dir(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    # Match real loader cast
    df["is_closed"] = df["is_closed"].astype(bool)
    return df


def test_normalise_address_handles_whitespace_and_junk() -> None:
    assert _normalise_address("  123  main  st  ") == "123 MAIN ST"
    # Too short and placeholder values are rejected (empty string)
    assert _normalise_address("n/a") == ""
    assert _normalise_address("None") == ""
    assert _normalise_address(".") == ""
    assert _normalise_address("") == ""


def test_successor_map_matches_unambiguous_address() -> None:
    df = _dir([
        {"account_number": "1001", "account_name": "*CLSD* ACME",
         "old_account_number": "A1", "address1": "123 Main St", "is_closed": True},
        {"account_number": "2001", "account_name": "ACME II",
         "old_account_number": "A2", "address1": "  123 main st ", "is_closed": False},
    ])
    succ, no_succ, meta = build_account_successor_map(df)
    assert succ == {"1001": "2001"}
    assert no_succ == set()
    assert meta["1001"]["successor_account"] == "2001"


def test_successor_map_no_open_listed_as_closed_no_successor() -> None:
    df = _dir([
        {"account_number": "1001", "account_name": "*CLSD* ACME",
         "old_account_number": "A1", "address1": "123 Main St", "is_closed": True},
    ])
    succ, no_succ, meta = build_account_successor_map(df)
    assert succ == {}
    assert no_succ == {"1001"}


def test_successor_map_ambiguous_skipped() -> None:
    df = _dir([
        {"account_number": "1001", "account_name": "*CLSD* ACME",
         "old_account_number": "A1", "address1": "123 Main St", "is_closed": True},
        {"account_number": "2001", "account_name": "ACME II",
         "old_account_number": "A2", "address1": "123 Main St", "is_closed": False},
        {"account_number": "2002", "account_name": "OTHER TENANT",
         "old_account_number": "A3", "address1": "123 Main St", "is_closed": False},
    ])
    succ, no_succ, meta = build_account_successor_map(df)
    # Ambiguous -> no remap entry, and not in closed_no_successor either
    assert "1001" not in succ
