"""Old↔new account-number resolution for AI conversation replies.

Reps type the OLD (BBANK2) account number — e.g. "51149" — but the warehouse
keys sales on the NEW account number (BACCT#, possibly alphanumeric). The
resolver must match either identifier and honor rep-scope restriction.
"""
from __future__ import annotations

import pandas as pd

from app.ui.views.conversations_view import _AiReplyWorker


def _asgn(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_resolves_old_number_to_new_account() -> None:
    asgn = _asgn([
        {"account_number": "PROHAR", "old_account_number": "51149",
         "account_name": "KMAF ASSOCIATES"},
        {"account_number": "ACME01", "old_account_number": "40636",
         "account_name": "ACME FLOORING"},
    ])
    # Rep typed the OLD number.
    resolved = _AiReplyWorker._resolve_referenced_accounts(asgn, ["51149"])
    assert set(resolved) == {"PROHAR"}
    assert resolved["PROHAR"]["old"] == "51149"
    assert resolved["PROHAR"]["name"] == "KMAF ASSOCIATES"
    assert resolved["PROHAR"]["closed"] == ""


def test_resolves_new_number_directly() -> None:
    asgn = _asgn([
        {"account_number": "PROHAR", "old_account_number": "51149",
         "account_name": "KMAF ASSOCIATES"},
    ])
    resolved = _AiReplyWorker._resolve_referenced_accounts(asgn, ["PROHAR"])
    assert set(resolved) == {"PROHAR"}


def test_strips_hash_and_flags_closed() -> None:
    asgn = _asgn([
        {"account_number": "NEW9", "old_account_number": "51149",
         "account_name": "*CLSD* OLD STORE"},
    ])
    resolved = _AiReplyWorker._resolve_referenced_accounts(asgn, ["#51149"])
    assert resolved["NEW9"]["closed"] == "1"
    # Leading '*' stripped from the display name.
    assert not resolved["NEW9"]["name"].startswith("*")


def test_restrict_to_accounts_blocks_other_reps_account() -> None:
    asgn = _asgn([
        {"account_number": "MINE", "old_account_number": "51149",
         "account_name": "MY ACCOUNT"},
        {"account_number": "THEIRS", "old_account_number": "51149",
         "account_name": "ANOTHER REP ACCOUNT"},
    ])
    # Same old number on two accounts, but rep only owns MINE.
    resolved = _AiReplyWorker._resolve_referenced_accounts(
        asgn, ["51149"], restrict_to_accounts={"MINE"}
    )
    assert set(resolved) == {"MINE"}


def test_unknown_number_returns_empty() -> None:
    asgn = _asgn([
        {"account_number": "PROHAR", "old_account_number": "51149",
         "account_name": "KMAF ASSOCIATES"},
    ])
    assert _AiReplyWorker._resolve_referenced_accounts(asgn, ["99999"]) == {}
