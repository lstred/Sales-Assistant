"""Smoke tests that don't need a database."""

from app.config.models import AppConfig, DatabaseConfig
from app.data.loaders import fiscal_year_for
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
    # Jan 2026 → FY 2026 (still in FY26 which started Feb 2025)
    assert fiscal_year_for(date(2026, 1, 15)) == 2026
    # Feb 2026 → FY 2027 (FY27 starts Feb 2026)
    assert fiscal_year_for(date(2026, 2, 1)) == 2027
    # May 2026 → FY 2027
    assert fiscal_year_for(date(2026, 5, 13)) == 2027
