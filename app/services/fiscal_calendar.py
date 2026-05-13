"""NRF fiscal calendar service.

Pattern:
* Fiscal year starts in **February**.
* Months follow a repeating **4-4-5** week pattern (4 weeks, 4 weeks,
  5 weeks). 12 months × that pattern = 52 weeks (364 days).
* **Every fiscal month always starts on a Sunday.**
* FY 2027 anchor: Sunday **February 1, 2026** is the first day of FY27 M1.
* Occasionally the final month (January) is **6 weeks** instead of 5 to
  realign with the calendar year. This is rare; the user tracks it manually
  via overrides persisted in ``settings_kv`` (key
  ``fiscal.six_week_january.<FY>``).

Notes:
* FY label convention follows :func:`fiscal_year_for` — a date in Feb 2026
  belongs to **FY 2027**.
* Period numbering: P1 = February, P12 = January (matches
  ``ClydeMarketingHistory.SalesPeriod1..12``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

# ---------------------------------------------------------------- constants
FY_ANCHOR_DATE: date = date(2026, 2, 1)
"""Sunday that begins the FY referenced by ``FY_ANCHOR_LABEL``."""

FY_ANCHOR_LABEL: int = 2027
"""Fiscal year corresponding to ``FY_ANCHOR_DATE``."""

WEEKS_PATTERN: tuple[int, ...] = (4, 4, 5, 4, 4, 5, 4, 4, 5, 4, 4, 5)
"""Default weeks-per-month for periods 1..12 (Feb..Jan)."""

MONTH_NAMES: tuple[str, ...] = (
    "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December", "January",
)


# ---------------------------------------------------------------- dataclasses
@dataclass(frozen=True)
class FiscalPeriod:
    fiscal_year: int
    period: int            # 1..12
    name: str              # e.g. "February"
    start: date            # inclusive
    end: date              # inclusive
    weeks: int

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end


# ---------------------------------------------------------------- helpers
def fiscal_year_for(d: date) -> int:
    """Calendar date → fiscal year. Feb–Dec → calendar+1; Jan → calendar."""
    return d.year + 1 if d.month >= 2 else d.year


def _weeks_for_year(fiscal_year: int, six_week_january_years: Iterable[int] = ()) -> tuple[int, ...]:
    """Return the 12-tuple of weeks per period, applying the rare
    6-week-January override if this FY is in ``six_week_january_years``."""
    weeks = list(WEEKS_PATTERN)
    if fiscal_year in six_week_january_years:
        weeks[-1] = 6
    return tuple(weeks)


def fy_start_date(
    fiscal_year: int,
    six_week_january_years: Iterable[int] = (),
) -> date:
    """First Sunday of the given fiscal year (period 1, day 1).

    Walks forward/backward from :data:`FY_ANCHOR_DATE` using the configured
    week pattern and any 6-week-January overrides for intermediate years.
    """
    sw = set(six_week_january_years)
    if fiscal_year == FY_ANCHOR_LABEL:
        return FY_ANCHOR_DATE
    if fiscal_year > FY_ANCHOR_LABEL:
        d = FY_ANCHOR_DATE
        for fy in range(FY_ANCHOR_LABEL, fiscal_year):
            d = d + timedelta(days=sum(_weeks_for_year(fy, sw)) * 7)
        return d
    # fiscal_year < anchor: walk backward
    d = FY_ANCHOR_DATE
    for fy in range(FY_ANCHOR_LABEL - 1, fiscal_year - 1, -1):
        d = d - timedelta(days=sum(_weeks_for_year(fy, sw)) * 7)
    return d


def build_fiscal_year(
    fiscal_year: int,
    six_week_january_years: Iterable[int] = (),
) -> list[FiscalPeriod]:
    """Build all 12 :class:`FiscalPeriod`s for the given fiscal year."""
    sw = set(six_week_january_years)
    weeks = _weeks_for_year(fiscal_year, sw)
    periods: list[FiscalPeriod] = []
    cursor = fy_start_date(fiscal_year, sw)
    for i, w in enumerate(weeks):
        start = cursor
        end = cursor + timedelta(days=w * 7 - 1)
        periods.append(
            FiscalPeriod(
                fiscal_year=fiscal_year,
                period=i + 1,
                name=MONTH_NAMES[i],
                start=start,
                end=end,
                weeks=w,
            )
        )
        cursor = end + timedelta(days=1)
    return periods


def find_period(
    d: date,
    six_week_january_years: Iterable[int] = (),
) -> FiscalPeriod:
    """Return the :class:`FiscalPeriod` containing ``d``."""
    fy = fiscal_year_for(d)
    sw = set(six_week_january_years)
    # Date may fall in fy or fy-1 if calendar Jan stretches; build both if needed
    for candidate in (fy, fy - 1, fy + 1):
        for p in build_fiscal_year(candidate, sw):
            if p.contains(d):
                return p
    raise ValueError(f"No fiscal period found for {d!r}")


def period_for_invoice_yyyymmdd(
    yyyymmdd: int,
    six_week_january_years: Iterable[int] = (),
) -> FiscalPeriod:
    """Convenience: convert an INVOICE_DATE_YYYYMMDD numeric to a period."""
    s = str(int(yyyymmdd))
    if len(s) != 8:
        raise ValueError(f"Expected YYYYMMDD, got {yyyymmdd!r}")
    return find_period(date(int(s[0:4]), int(s[4:6]), int(s[6:8])), six_week_january_years)
