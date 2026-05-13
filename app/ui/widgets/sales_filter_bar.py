"""Shared filter bar: cost-center selector + date range + Run button.

Used by Sales-by-Rep, Sales-by-Cost-Center, Weekly Email, and Ask the AI.

UX rules baked in here so every screen behaves the same:

* Cost centers auto-load on first show; "All" is selected by default.
* Default date range = the **last 12 fully-completed fiscal periods**
  (≈ a rolling year ending at the most-recent closed fiscal month). Optional
  "vs prior year" comparison spans the same 12 periods one fiscal year back.
* "Run" auto-fires the moment data is ready, so the parent screen never
  shows up empty.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable

from PySide6.QtCore import QDate, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import pandas as pd

from app.config.models import AppConfig, DatabaseConfig
from app.data.loaders import load_blended_sales
from app.services.fiscal_calendar import (
    last_full_period,
    last_n_full_periods_range,
)
from app.storage import sales_cache
from app.ui.theme import TEXT_MUTED
from app.ui.widgets.cc_selector import CostCenterSelector


class _SalesLoader(QThread):
    loaded = Signal(object, object)  # current_df, prior_df (None if not requested)
    failed = Signal(str)

    def __init__(
        self,
        db: DatabaseConfig,
        start: date,
        end: date,
        ccs: list[str],
        prior_start: date | None,
        prior_end: date | None,
        six_week_january_years: list[int] | None = None,
        code_prefix: str = "",
    ) -> None:
        super().__init__()
        self._db = db
        self._start, self._end, self._ccs = start, end, ccs
        self._prior_start, self._prior_end = prior_start, prior_end
        self._sw = list(six_week_january_years or ())
        self._code_prefix = (code_prefix or "").strip()

    def run(self) -> None:  # noqa: D401
        try:
            cur = load_blended_sales(
                self._db, self._start, self._end, self._ccs or None, self._sw,
                self._code_prefix,
            )
            prior = None
            if self._prior_start is not None and self._prior_end is not None:
                prior = load_blended_sales(
                    self._db, self._prior_start, self._prior_end,
                    self._ccs or None, self._sw, self._code_prefix,
                )
            self.loaded.emit(cur, prior)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class SalesFilterBar(QFrame):
    run_requested = Signal(object, object, list)
    sales_loaded = Signal(object)               # current df (back-compat)
    sales_loaded_with_prior = Signal(object, object)  # current, prior (or None)
    failed = Signal(str)

    def __init__(
        self,
        get_db: Callable[[], DatabaseConfig],
        cfg: AppConfig | None = None,
        parent: QWidget | None = None,
        *,
        autoload: bool = True,
        autorun: bool = True,
        code_prefix_filter: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setMinimumWidth(300)
        self.setMaximumWidth(340)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._get_db = get_db
        self._cfg = cfg
        self._loader: _SalesLoader | None = None
        self._autorun = autorun
        self._has_autorun = False
        self._last_cache_ts = None  # type: datetime | None
        self._code_prefix_filter = (code_prefix_filter or "").strip()

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        self.cc = CostCenterSelector(
            get_db,
            autoload=autoload,
            select_all_after_load=True,
            code_prefix_filter=code_prefix_filter,
        )
        self.cc.loaded.connect(self._on_cc_loaded)
        root.addWidget(self.cc, 1)

        # Date range
        date_label = QLabel("Date Range (invoice date)")
        date_label.setStyleSheet("font-weight: 600;")
        root.addWidget(date_label)

        # Smart defaults: last full year of completed fiscal months
        sw = self._cfg.fiscal.six_week_january_years if self._cfg else []
        try:
            default_start, default_end = last_n_full_periods_range(date.today(), 12, sw)
        except Exception:  # noqa: BLE001
            today = QDate.currentDate()
            default_start = (today.addDays(-365)).toPython()
            default_end = today.toPython()

        self.start_edit = QDateEdit()
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDate(QDate(default_start.year, default_start.month, default_start.day))
        self.end_edit = QDateEdit()
        self.end_edit.setCalendarPopup(True)
        self.end_edit.setDate(QDate(default_end.year, default_end.month, default_end.day))

        d_row1 = QHBoxLayout()
        d_row1.addWidget(QLabel("From"))
        d_row1.addWidget(self.start_edit, 1)
        root.addLayout(d_row1)
        d_row2 = QHBoxLayout()
        d_row2.addWidget(QLabel("To  "))
        d_row2.addWidget(self.end_edit, 1)
        root.addLayout(d_row2)

        # Quick presets — three per row so labels don't clip
        for labels in (
            (("Last full FM", "lfm"), ("Last 3 FM", 3), ("Last 6 FM", 6)),
            (("Rolling year", 12), ("YTD", "ytd"), ("Last 30d", -30)),
        ):
            row = QHBoxLayout()
            row.setSpacing(6)
            for label, kind in labels:
                b = QPushButton(label)
                b.clicked.connect(lambda _=False, k=kind: self._apply_preset(k))
                row.addWidget(b, 1)
            root.addLayout(row)

        self.compare_prior = QCheckBox("Also load prior year (for comparison)")
        self.compare_prior.setChecked(True)
        root.addWidget(self.compare_prior)

        self.status = QLabel("Loading…")
        self.status.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self.cache_label = QLabel("")
        self.cache_label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        self.cache_label.setWordWrap(True)
        root.addWidget(self.cache_label)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self.run_btn = QPushButton("Run")
        self.run_btn.setProperty("primary", True)
        self.run_btn.clicked.connect(lambda: self._run(force_refresh=False))
        self.refresh_btn = QPushButton("Refresh from DB")
        self.refresh_btn.clicked.connect(lambda: self._run(force_refresh=True))
        btn_row.addWidget(self.run_btn, 1)
        btn_row.addWidget(self.refresh_btn, 1)
        root.addLayout(btn_row)

    # --------------------------------------------------------------- public
    def selected_codes(self) -> list[str]:
        return self.cc.selected_codes()

    def date_range(self) -> tuple[date, date]:
        s = self.start_edit.date().toPython()
        e = self.end_edit.date().toPython()
        return s, e

    # --------------------------------------------------------------- presets
    def _apply_preset(self, kind) -> None:
        sw = self._cfg.fiscal.six_week_january_years if self._cfg else []
        today = date.today()
        if kind == "lfm":
            p = last_full_period(today, sw)
            self._set_dates(p.start, p.end)
        elif isinstance(kind, int) and kind > 0:  # last N full fiscal months
            s, e = last_n_full_periods_range(today, kind, sw)
            self._set_dates(s, e)
        elif kind == "ytd":
            self._set_dates(date(today.year, 1, 1), today)
        elif isinstance(kind, int) and kind < 0:
            self._set_dates(today.fromordinal(today.toordinal() + kind), today)

    def _set_dates(self, s: date, e: date) -> None:
        self.start_edit.setDate(QDate(s.year, s.month, s.day))
        self.end_edit.setDate(QDate(e.year, e.month, e.day))

    # --------------------------------------------------------------- run
    def _on_cc_loaded(self, _count: int) -> None:
        if self._autorun and not self._has_autorun:
            self._has_autorun = True
            self._run(force_refresh=False)

    def _run(self, *, force_refresh: bool = False) -> None:
        ccs = self.selected_codes()
        s, e = self.date_range()
        if e < s:
            self.status.setText("End date must be on/after start date.")
            return
        prior_start = prior_end = None
        if self.compare_prior.isChecked():
            # Same span, shifted exactly one calendar year back (handles leap-day).
            try:
                prior_start = s.replace(year=s.year - 1)
            except ValueError:
                prior_start = s - timedelta(days=365)
            try:
                prior_end = e.replace(year=e.year - 1)
            except ValueError:
                prior_end = e - timedelta(days=365)

        # Cache hit short-circuit (skipped on explicit Refresh).
        if not force_refresh:
            cur_key = sales_cache.make_key(s, e, ccs, self._code_prefix_filter)
            cur_hit = sales_cache.get(cur_key)
            prior_hit = None
            if prior_start is not None and prior_end is not None:
                prior_hit = sales_cache.get(
                    sales_cache.make_key(
                        prior_start, prior_end, ccs, self._code_prefix_filter
                    )
                )
            if cur_hit and (prior_start is None or prior_hit):
                cur_df, ts = cur_hit
                prior_df = prior_hit[0] if prior_hit else None
                self._last_cache_ts = ts
                self.status.setText(
                    f"{len(cur_df):,} cached lines"
                    + (f" · prior {len(prior_df):,}" if prior_df is not None else "")
                )
                self._refresh_cache_label()
                self.sales_loaded.emit(cur_df)
                self.sales_loaded_with_prior.emit(cur_df, prior_df)
                return

        scope = ", ".join(ccs) if ccs else "all CCs"
        self.status.setText(f"Loading {s} → {e} for {scope}…")
        self.run_btn.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.run_requested.emit(s, e, ccs)

        sw = list(self._cfg.fiscal.six_week_january_years) if self._cfg else []
        self._loader = _SalesLoader(
            self._get_db(), s, e, ccs, prior_start, prior_end, sw,
            self._code_prefix_filter,
        )
        self._loader.loaded.connect(self._on_loaded)
        self._loader.failed.connect(self._on_failed)
        self._loader.start()

    def _on_loaded(self, df: pd.DataFrame, prior: pd.DataFrame | None) -> None:
        self.run_btn.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        # Persist to cache so a future open is instant.
        s, e = self.date_range()
        ccs = self.selected_codes()
        try:
            ts = sales_cache.put(
                sales_cache.make_key(s, e, ccs, self._code_prefix_filter), df
            )
            self._last_cache_ts = ts
            if prior is not None:
                try:
                    ps = s.replace(year=s.year - 1)
                    pe = e.replace(year=e.year - 1)
                except ValueError:
                    ps, pe = s - timedelta(days=365), e - timedelta(days=365)
                sales_cache.put(
                    sales_cache.make_key(ps, pe, ccs, self._code_prefix_filter),
                    prior,
                )
        except Exception:  # noqa: BLE001 — caching failures are non-fatal
            pass
        suffix = ""
        if prior is not None:
            suffix = f" · prior {len(prior):,} lines"
        self.status.setText(f"{len(df):,} invoiced lines{suffix}.")
        self._refresh_cache_label()
        self.sales_loaded.emit(df)
        self.sales_loaded_with_prior.emit(df, prior)

    def _on_failed(self, msg: str) -> None:
        self.run_btn.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        self.status.setText(f"Failed — {msg}")
        self.failed.emit(msg)

    def _refresh_cache_label(self) -> None:
        if self._last_cache_ts is None:
            self.cache_label.setText("")
            return
        self.cache_label.setText(
            f"Last refreshed {self._last_cache_ts.strftime('%b %d, %Y at %I:%M %p').lstrip('0')}"
        )
