"""Tests for app.services.marketing_programs."""
from __future__ import annotations

import pandas as pd

from app.services.marketing_programs import (
    UNCATEGORIZED,
    account_program_maps,
    build_program_directory,
    per_account_program_lines,
    summarise_for_ai,
)


def _types_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"program_code": "CCA01", "program_desc": "CCA Spring Buy"},
            {"program_code": "NRF02", "program_desc": "NRF Q2 Rebate"},
            {"program_code": "MISC9", "program_desc": "Misc Promo"},
        ]
    )


def _placements_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"account_number": "1001", "program_code": "CCA01", "program_desc": "CCA Spring Buy"},
            {"account_number": "1002", "program_code": "CCA01", "program_desc": "CCA Spring Buy"},
            {"account_number": "1001", "program_code": "NRF02", "program_desc": "NRF Q2 Rebate"},
            {"account_number": "1003", "program_code": "MISC9", "program_desc": "Misc Promo"},
        ]
    )


def test_build_program_directory_categorizes_and_stars() -> None:
    cats = {"CCA01": "CCA Buying Group", "NRF02": "NRF Rebate Program"}
    starred = ["CCA01"]
    dirmap = build_program_directory(_types_df(), cats, starred)

    assert dirmap["CCA01"]["category"] == "CCA Buying Group"
    assert dirmap["CCA01"]["starred"] is True
    assert dirmap["NRF02"]["category"] == "NRF Rebate Program"
    assert dirmap["NRF02"]["starred"] is False
    # Unmapped program falls into Uncategorized.
    assert dirmap["MISC9"]["category"] == UNCATEGORIZED
    assert dirmap["MISC9"]["starred"] is False


def test_summarise_for_ai_scopes_to_account_filter_and_highlights_starred() -> None:
    cats = {"CCA01": "CCA Buying Group", "NRF02": "NRF Rebate Program"}
    starred = ["CCA01"]
    # Scope: only account 1001 (drops MISC9 entirely).
    out = summarise_for_ai(
        _placements_df(),
        _types_df(),
        cats,
        starred,
        account_filter={"1001"},
    )
    assert "MARKETING PROGRAMS" in out
    assert "STARRED PROGRAMS" in out
    assert "CCA01" in out
    assert "NRF02" in out
    # MISC9 was on account 1003 — must not appear when scoped to 1001.
    assert "MISC9" not in out
    # Category roll-up must list both mapped categories.
    assert "CCA Buying Group" in out
    assert "NRF Rebate Program" in out


def test_summarise_for_ai_returns_empty_when_no_placements_or_no_match() -> None:
    assert summarise_for_ai(None, None, None, None) == ""
    assert summarise_for_ai(pd.DataFrame(), _types_df(), {}, []) == ""
    # Account filter that matches nothing.
    out = summarise_for_ai(
        _placements_df(), _types_df(), {}, [], account_filter={"9999"}
    )
    assert out == ""


def test_per_account_program_lines_only_starred_filters_accounts() -> None:
    cats = {"CCA01": "CCA Buying Group"}
    starred = ["CCA01"]
    # Account 1003 has only MISC9 (not starred) -> must be omitted.
    out = per_account_program_lines(
        _placements_df(),
        _types_df(),
        cats,
        starred,
        ["1001", "1002", "1003"],
        account_labels={"1001": "ABC FLOORING", "1002": "XYZ TILE"},
        only_starred=True,
    )
    assert "1001" in out
    assert "ABC FLOORING" in out
    assert "1002" in out
    assert "1003" not in out
    # Starred programs should have a trailing '*' marker.
    assert "CCA01*" in out


def test_account_program_maps_only_starred_excludes_non_starred() -> None:
    cats = {"CCA01": "CCA Buying Group", "NRF02": "NRF Rebate Program"}
    starred = ["CCA01"]  # NRF02 not starred
    cat_map, code_map = account_program_maps(
        _placements_df(), _types_df(), cats, starred, only_starred=True
    )
    # 1001 is in CCA01 (starred) AND NRF02 (not starred) — only CCA should appear.
    assert cat_map["1001"] == {"CCA Buying Group"}
    assert code_map["1001"] == {"CCA01"}
    # 1002 is in CCA01 only.
    assert cat_map["1002"] == {"CCA Buying Group"}
    # 1003 is only in MISC9 (not starred) — must be absent entirely.
    assert "1003" not in cat_map
    assert "1003" not in code_map


def test_account_program_maps_all_programs_when_not_only_starred() -> None:
    cats = {"CCA01": "CCA Buying Group", "NRF02": "NRF Rebate Program"}
    cat_map, code_map = account_program_maps(
        _placements_df(), _types_df(), cats, [], only_starred=False
    )
    assert cat_map["1001"] == {"CCA Buying Group", "NRF Rebate Program"}
    assert code_map["1001"] == {"CCA01", "NRF02"}
    assert cat_map["1003"] == {UNCATEGORIZED}
