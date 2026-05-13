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


def test_unpivot_old_sales_uses_rep_map() -> None:
    import pandas as pd
    from datetime import date
    from app.data.loaders import _unpivot_old_sales, LEGACY_REP_LABEL

    raw = pd.DataFrame([{
        "marketing_code": "X",
        "cost_center": "010",
        "cost_center_name": "CARPET RESIDENTIAL",
        "fiscal_year": 2026,
        "old_customer_number": "OLD-1",
        "account_number": "ACCT-1",
        "account_name": "Test Customer",
        **{f"SalesPeriod{i}": (1000 if i == 4 else 0) for i in range(1, 13)},
        **{f"CostsPeriod{i}": (700 if i == 4 else 0) for i in range(1, 13)},
        "TotalSales": 1000, "TotalCost": 700, "Profit": 300,
    }])
    rep_map = {("ACCT-1", "010"): "JANE DOE"}
    out = _unpivot_old_sales(
        raw, date(2025, 1, 1), date(2025, 12, 31), None, [], rep_map
    )
    assert not out.empty
    assert (out["salesperson_desc"] == "JANE DOE").all()
    assert (out["account_number"] == "ACCT-1").all()
    assert out["revenue"].sum() == 1000.0
    assert out["gross_profit"].sum() == 300.0

    # Without rep map ? falls back to legacy label
    out2 = _unpivot_old_sales(
        raw, date(2025, 1, 1), date(2025, 12, 31), None, [], {}
    )
    assert (out2["salesperson_desc"] == LEGACY_REP_LABEL).all()


def test_sales_cache_round_trip(tmp_path, monkeypatch) -> None:
    import pandas as pd
    from datetime import date
    monkeypatch.setattr("app.storage.sales_cache.state_db_path", lambda: tmp_path / "cache.sqlite")
    from app.storage import sales_cache

    df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    key = sales_cache.make_key(date(2025, 1, 1), date(2025, 1, 31), ["010", "011"])
    assert sales_cache.get(key) is None
    ts = sales_cache.put(key, df)
    hit = sales_cache.get(key)
    assert hit is not None
    cached_df, cached_ts = hit
    assert list(cached_df.columns) == ["a", "b"]
    assert len(cached_df) == 3
    assert cached_df["a"].sum() == 6
    # Stored timestamp is truncated to seconds in SQLite.
    assert abs((cached_ts - ts).total_seconds()) < 1.0
    assert sales_cache.has_any() is True
    n = sales_cache.clear_all()
    assert n == 1
    assert sales_cache.get(key) is None


def test_make_key_is_order_independent() -> None:
    from datetime import date
    from app.storage import sales_cache
    a = sales_cache.make_key(date(2025, 1, 1), date(2025, 1, 31), ["011", "010"])
    b = sales_cache.make_key(date(2025, 1, 1), date(2025, 1, 31), ["010", "011"])
    assert a == b


def test_make_key_includes_prefix() -> None:
    from datetime import date
    from app.storage import sales_cache
    a = sales_cache.make_key(date(2025, 1, 1), date(2025, 1, 31), ["010"], "")
    b = sales_cache.make_key(date(2025, 1, 1), date(2025, 1, 31), ["010"], "0")
    assert a != b


def test_invoice_cache_serves_immutable_months_from_disk(tmp_path, monkeypatch) -> None:
    """Past months must be fetched from the warehouse exactly once,
    then served from local SQLite forever."""
    import pandas as pd
    from datetime import date
    monkeypatch.setattr(
        "app.storage.invoice_cache.state_db_path",
        lambda: tmp_path / "ic.sqlite",
    )
    from app.storage import invoice_cache

    fetch_count = {"n": 0}

    def fake_fetch(db, year, month, code_prefix):
        fetch_count["n"] += 1
        return pd.DataFrame([{
            "invoice_yyyymmdd": year * 10000 + month * 100 + 15,
            "account_number": "A1",
            "cost_center": "010",
            "salesperson_desc": "JANE",
            "invoice_number": 1, "order_number": 1, "line_number": 1,
            "revenue": 100.0, "gross_profit": 30.0,
        }])

    monkeypatch.setattr(invoice_cache, "_fetch_month", fake_fetch)

    class _FrozenDate(date):
        @classmethod
        def today(cls):  # type: ignore[override]
            return date(2026, 6, 1)

    monkeypatch.setattr(invoice_cache, "date", _FrozenDate)

    df1 = invoice_cache.get_for_range(None, date(2025, 1, 1), date(2025, 3, 31), "0")
    assert len(df1) == 3            # one row per cached month
    assert fetch_count["n"] == 3

    df2 = invoice_cache.get_for_range(None, date(2025, 1, 1), date(2025, 3, 31), "0")
    assert len(df2) == 3
    assert fetch_count["n"] == 3    # no new warehouse calls

    # Different prefix → separate cache slot, fresh fetches.
    df3 = invoice_cache.get_for_range(None, date(2025, 1, 1), date(2025, 1, 31), "1")
    assert len(df3) == 1
    assert fetch_count["n"] == 4


class _FrozenDate(date):
    """Helper to monkey-patch ``invoice_cache.date.today()``."""

    _fixed: date | None = None

    def __new__(cls, fixed: date):
        inst = date.__new__(cls, fixed.year, fixed.month, fixed.day)
        inst._fixed = fixed
        return inst

    @classmethod
    def today(cls):  # type: ignore[override]
        return cls._fixed if cls._fixed is not None else date.today()
