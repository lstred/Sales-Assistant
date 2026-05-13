"""Smoke tests that don't need a database."""

from app.config.models import AppConfig, DatabaseConfig
from app.data.loaders import fiscal_year_for
from app.services.fiscal_calendar import (
    FY_ANCHOR_DATE,
    FY_ANCHOR_LABEL,
    build_fiscal_year,
    find_period,
    fy_start_date,
)
from datetime import date


def test_default_config_serializes() -> None:
    cfg = AppConfig()
    assert cfg.schema_version == 1
    j = cfg.model_dump(mode="json")
    rt = AppConfig.model_validate(j)
    assert rt == cfg


def test_db_connection_string_round_trip() -> None:
    db = DatabaseConfig()
    s = db.odbc_connection_string()
    assert "Driver={ODBC Driver 18 for SQL Server}" in s
    assert "Server=NRFVMSSQL04" in s
    assert "Trusted_Connection=Yes" in s


def test_fiscal_year_starts_in_february() -> None:
    assert fiscal_year_for(date(2026, 1, 15)) == 2026
    assert fiscal_year_for(date(2026, 2, 1)) == 2027
    assert fiscal_year_for(date(2026, 5, 13)) == 2027


def test_fiscal_anchor_is_a_sunday() -> None:
    # Anchor + every period start must be a Sunday (weekday() == 6)
    assert FY_ANCHOR_DATE.weekday() == 6
    for p in build_fiscal_year(FY_ANCHOR_LABEL):
        assert p.start.weekday() == 6, f"{p.name} start {p.start} is not Sunday"


def test_445_pattern_yields_52_weeks() -> None:
    periods = build_fiscal_year(FY_ANCHOR_LABEL)
    assert sum(p.weeks for p in periods) == 52
    assert [p.weeks for p in periods] == [4, 4, 5, 4, 4, 5, 4, 4, 5, 4, 4, 5]


def test_six_week_january_override_extends_to_53() -> None:
    periods = build_fiscal_year(FY_ANCHOR_LABEL, [FY_ANCHOR_LABEL])
    assert periods[-1].weeks == 6
    assert sum(p.weeks for p in periods) == 53


def test_walking_back_and_forward_from_anchor() -> None:
    # Without overrides, FY28 starts exactly 364 days after FY27 anchor
    from datetime import timedelta
    assert fy_start_date(FY_ANCHOR_LABEL + 1) == FY_ANCHOR_DATE + timedelta(days=364)
    assert fy_start_date(FY_ANCHOR_LABEL - 1) == FY_ANCHOR_DATE - timedelta(days=364)


def test_find_period_for_known_dates() -> None:
    p1 = find_period(date(2026, 2, 1))   # first day of FY27 P1
    assert p1.fiscal_year == 2027 and p1.period == 1 and p1.name == "February"
    p2 = find_period(date(2026, 3, 1))   # second period starts here
    assert p2.period == 2 and p2.name == "March"
    p3 = find_period(date(2026, 5, 13))  # mid-May → FY27 P4 (May, weeks=4)
    assert p3.fiscal_year == 2027 and p3.name == "May"

