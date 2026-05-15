"""Shared filter bar: cost-center selector + date range + Run button.

Used by Sales-by-Rep, Sales-by-Cost-Center, Weekly Email, and Ask the AI.

UX rules baked in here so every screen behaves the same:

* Cost centers auto-load on first show; "All" is selected by default.
* Default date range = fiscal YTD (FY start → end of last completed period).
* If a per-page default has been saved (page_id argument), it is applied on
  first load instead of the fallback YTD default.
* "Run" auto-fires the moment data is ready, so the parent screen never
  shows up empty.
* Relative date tokens (e.g. "today", "fiscal_ytd_start") are resolved fresh
  every time the page loads, so saved defaults stay correct over time.
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
    QMenu,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import pandas as pd

from app.config.models import AppConfig, DatabaseConfig, PageFilterDefault
from app.config.store import save_config
from app.data.loaders import load_blended_sales
from app.services.fiscal_calendar import (
    last_full_period,
    last_n_full_periods_range,
    fy_start_date,
)
from app.services.singleflight import sales_singleflight
from app.storage import sales_cache
from app.ui.theme import ACCENT, TEXT_MUTED
from app.ui.widgets.cc_selector import CostCenterSelector



# ---------------------------------------------------------------------------
# Relative date tokens — resolved to a concrete date each time the app loads.
# ---------------------------------------------------------------------------
_RELATIVE_OPTIONS: list[tuple[str, str]] = [
    ("today",               "Today"),
    ("yesterday",           "Yesterday"),
    ("1_week_ago",          "1 week ago"),
    ("start_this_month",    "Start of this month"),
    ("1_month_ago",         "1 month ago"),
    ("3_months_ago",        "3 months ago"),
    ("6_months_ago",        "6 months ago"),
    ("start_this_year",     "Start of calendar year"),
    ("fiscal_ytd_start",    "Start of fiscal year"),
    ("last_fm_start",       "Start of last full fiscal month"),
    ("last_fm_end",         "End of last full fiscal month"),
]


def resolve_relative_date(token: str, six_week_january_years: list[int] | None = None) -> date:
    """Resolve a relative-date token to a concrete ``date``.

    Falls back to ``date.today()`` for unknown tokens so the UI never crashes.
    """
    sw = list(six_week_january_years or ())
    today = date.today()
    if token == "today":
        return today
    if token == "yesterday":
        return today - timedelta(days=1)
    if token == "1_week_ago":
        return today - timedelta(weeks=1)
    if token == "start_this_month":
        return today.replace(day=1)
    if token == "1_month_ago":
        y, m = today.year, today.month - 1
        if m < 1:
            m, y = 12, y - 1
        return date(y, m, today.day)
    if token == "3_months_ago":
        y, m = today.year, today.month - 3
        while m < 1:
            m += 12
            y -= 1
        return date(y, m, today.day)
    if token == "6_months_ago":
        y, m = today.year, today.month - 6
        while m < 1:
            m += 12
            y -= 1
        return date(y, m, today.day)
    if token == "start_this_year":
        return date(today.year, 1, 1)
    if token == "fiscal_ytd_start":
        try:
            last_p = last_full_period(today, sw)
            return fy_start_date(last_p.fiscal_year, sw)
        except Exception:  # noqa: BLE001
            return date(today.year, 2, 1)
    if token == "last_fm_start":
        try:
            return last_full_period(today, sw).start
        except Exception:  # noqa: BLE001
            return today.replace(day=1)
    if token == "last_fm_end":
        try:
            return last_full_period(today, sw).end
        except Exception:  # noqa: BLE001
            return today
    return today  # unknown token


class _RelativeDateButton(QToolButton):
    """Small ▾ button that shows a relative-date menu next to a QDateEdit."""

    def __init__(
        self,
        target_getter: Callable[[], QDateEdit],
        sw_getter: Callable[[], list[int]],
        *,
        token_holder: list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setText("▾")
        self.setToolTip("Pick a relative date")
        self.setFixedWidth(26)
        self.setStyleSheet(
            "QToolButton { border: 1px solid #CBD5E1; border-radius: 6px; "
            "background: #F8FAFC; font-size: 11px; padding: 0 3px; }"
            "QToolButton:hover { background: #E2E8F0; }"
        )
        self._get_target = target_getter
        self._get_sw = sw_getter
        self._token_holder = token_holder
        self.clicked.connect(self._show_menu)

    def _show_menu(self) -> None:
        menu = QMenu(self)
        act = menu.addAction("Custom date (manual)")
        act.triggered.connect(lambda: self._apply(""))
        menu.addSeparator()
        for token, label in _RELATIVE_OPTIONS:
            a = menu.addAction(label)
            a.triggered.connect(lambda _=False, t=token: self._apply(t))
        menu.exec(self.mapToGlobal(self.rect().bottomLeft()))

    def _apply(self, token: str) -> None:
        self._token_holder[0] = token
        if not token:
            return  # leave date as-is, user will pick manually
        d = resolve_relative_date(token, self._get_sw())
        self._get_target().blockSignals(True)
        self._get_target().setDate(QDate(d.year, d.month, d.day))
        self._get_target().blockSignals(False)


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
            # Singleflight key — identical concurrent requests across views
            # collapse into one warehouse call. After the first call returns,
            # the others get the same DataFrame and the per-month cache is
            # already populated, so subsequent reads are instant.
            ccs_key = ",".join(sorted({str(c).strip() for c in (self._ccs or ()) if c}))
            cur_key = (
                "blended", self._start.isoformat(), self._end.isoformat(),
                ccs_key, self._code_prefix,
            )
            cur = sales_singleflight.do(
                cur_key,
                lambda: load_blended_sales(
                    self._db, self._start, self._end, self._ccs or None,
                    self._sw, self._code_prefix,
                ),
            )
            prior = None
            if self._prior_start is not None and self._prior_end is not None:
                prior_key = (
                    "blended", self._prior_start.isoformat(),
                    self._prior_end.isoformat(), ccs_key, self._code_prefix,
                )
                prior = sales_singleflight.do(
                    prior_key,
                    lambda: load_blended_sales(
                        self._db, self._prior_start, self._prior_end,
                        self._ccs or None, self._sw, self._code_prefix,
                    ),
                )
            self.loaded.emit(cur, prior)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class SalesFilterBar(QFrame):
    run_requested = Signal(object, object, list)
    sales_loaded = Signal(object)               # current df (back-compat)
    sales_loaded_with_prior = Signal(object, object)  # current, prior (or None)
    failed = Signal(str)
    busy_state_changed = Signal(str)  # "loading" | "done" | "failed"

    def __init__(
        self,
        get_db: Callable[[], DatabaseConfig],
        cfg: AppConfig | None = None,
        parent: QWidget | None = None,
        *,
        autoload: bool = True,
        autorun: bool = True,
        code_prefix_filter: str | None = None,
        page_id: str = "",
    ) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setMinimumWidth(300)
        self.setMaximumWidth(340)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._get_db = get_db
        self._cfg = cfg
        self._page_id = page_id.strip()
        # Hold strong refs to *every* in-flight loader so PySide6 can't GC
        # the QThread mid-run (which is the classic native-crash trigger).
        # Threads are removed from the list when their ``finished`` signal fires.
        self._loaders: list[_SalesLoader] = []
        self._autorun = autorun
        self._has_autorun = False
        self._last_cache_ts: datetime | None = None
        self._code_prefix_filter = (code_prefix_filter or "").strip()
        # Mutable 1-element lists so _RelativeDateButton can write back to us
        self._start_token: list[str] = [""]
        self._end_token: list[str] = [""]

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        self.cc = CostCenterSelector(
            get_db,
            autoload=autoload,
            select_all_after_load=True,
            code_prefix_filter=code_prefix_filter,
            default_selected=(
                list(self._cfg.defaults.cost_centers)
                if self._cfg is not None and self._cfg.defaults.cost_centers
                else None
            ),
        )
        self.cc.loaded.connect(self._on_cc_loaded)
        root.addWidget(self.cc, 1)

        # ── Date range ──────────────────────────────────────────────────────
        date_label = QLabel("Date Range (invoice date)")
        date_label.setStyleSheet("font-weight: 600;")
        root.addWidget(date_label)

        sw = self._cfg.fiscal.six_week_january_years if self._cfg else []
        try:
            _last_p = last_full_period(date.today(), sw)
            default_start = fy_start_date(_last_p.fiscal_year, sw)
            default_end = _last_p.end
        except Exception:  # noqa: BLE001
            default_end = date.today()
            default_start = date(default_end.year, 1, 1)

        self.start_edit = QDateEdit()
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDate(QDate(default_start.year, default_start.month, default_start.day))
        # Manual calendar pick clears the relative token for that field
        self.start_edit.dateChanged.connect(lambda _: self._clear_token(self._start_token))

        self.end_edit = QDateEdit()
        self.end_edit.setCalendarPopup(True)
        self.end_edit.setDate(QDate(default_end.year, default_end.month, default_end.day))
        self.end_edit.dateChanged.connect(lambda _: self._clear_token(self._end_token))

        sw_getter = lambda: (self._cfg.fiscal.six_week_january_years if self._cfg else [])
        start_rel_btn = _RelativeDateButton(
            lambda: self.start_edit, sw_getter, token_holder=self._start_token,
        )
        end_rel_btn = _RelativeDateButton(
            lambda: self.end_edit, sw_getter, token_holder=self._end_token,
        )

        d_row1 = QHBoxLayout()
        d_row1.setSpacing(4)
        d_row1.addWidget(QLabel("From"))
        d_row1.addWidget(self.start_edit, 1)
        d_row1.addWidget(start_rel_btn)
        root.addLayout(d_row1)

        d_row2 = QHBoxLayout()
        d_row2.setSpacing(4)
        d_row2.addWidget(QLabel("To  "))
        d_row2.addWidget(self.end_edit, 1)
        d_row2.addWidget(end_rel_btn)
        root.addLayout(d_row2)

        # Quick presets — three per row so labels don't clip
        for labels in (
            (("Last full FM", "lfm"), ("Last 3 FM", 3), ("Last 6 FM", 6)),
            (("Rolling year", 12), ("YTD", "ytd"), ("Last 30d", -30)),
        ):
            row = QHBoxLayout()
            row.setSpacing(5)
            for label, kind in labels:
                b = QPushButton(label)
                b.clicked.connect(lambda _=False, k=kind: self._apply_preset(k))
                row.addWidget(b, 1)
            root.addLayout(row)

        self.compare_prior = QCheckBox("Also load prior year (for comparison)")
        self.compare_prior.setChecked(
            self._cfg.defaults.vs_prior_year if self._cfg is not None else True
        )
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
        btn_row.addWidget(self.run_btn, 1)
        root.addLayout(btn_row)

        # "Save as default for this page" — only shown when page_id is set
        if self._page_id:
            self.save_default_btn = QPushButton("⭐  Save as default")
            self.save_default_btn.setToolTip(
                "Save the current filter selection as the default for this page.\n"
                "Relative date options (e.g. 'Yesterday') are stored as tokens so\n"
                "they resolve correctly each time you open the app."
            )
            self.save_default_btn.setStyleSheet(
                "QPushButton { font-size: 11px; color: #0F172A; "
                "border: 1px solid #CBD5E1; border-radius: 6px; "
                "padding: 4px 8px; background: #F8FAFC; }"
                "QPushButton:hover { background: #E2E8F0; border-color: #94A3B8; }"
                "QPushButton:pressed { background: #DBEAFE; }"
            )
            self.save_default_btn.clicked.connect(self._save_page_default)
            self._saved_label = QLabel("")
            self._saved_label.setStyleSheet(f"color: {ACCENT}; font-size: 10px;")
            root.addWidget(self.save_default_btn)
            root.addWidget(self._saved_label)

        # Apply saved page default (if any) now that all widgets are built
        if self._page_id and self._cfg is not None:
            pd_saved = self._cfg.page_defaults.get(self._page_id)
            if pd_saved is not None:
                self._apply_page_default(pd_saved)

    # --------------------------------------------------------------- public
    def selected_codes(self) -> list[str]:
        return self.cc.selected_codes()

    def date_range(self) -> tuple[date, date]:
        s = self.start_edit.date().toPython()
        e = self.end_edit.date().toPython()
        return s, e

    # --------------------------------------------------------------- presets
    def _apply_preset(self, kind) -> None:
        # Presets set absolute dates — clear both relative tokens
        self._start_token[0] = ""
        self._end_token[0] = ""
        sw = self._cfg.fiscal.six_week_january_years if self._cfg else []
        today = date.today()
        if kind == "lfm":
            p = last_full_period(today, sw)
            self._set_dates(p.start, p.end)
        elif isinstance(kind, int) and kind > 0:  # last N full fiscal months
            s, e = last_n_full_periods_range(today, kind, sw)
            self._set_dates(s, e)
        elif kind == "ytd":
            # Fiscal YTD: FY start → end of last completed fiscal period
            try:
                last_p = last_full_period(today, sw)
                fy_s = fy_start_date(last_p.fiscal_year, sw)
                self._set_dates(fy_s, last_p.end)
            except Exception:  # noqa: BLE001
                self._set_dates(date(today.year, 1, 1), today)
        elif isinstance(kind, int) and kind < 0:
            self._set_dates(today + timedelta(days=kind), today)

    def _set_dates(self, s: date, e: date) -> None:
        self.start_edit.setDate(QDate(s.year, s.month, s.day))
        self.end_edit.setDate(QDate(e.year, e.month, e.day))

    @staticmethod
    def _clear_token(token_holder: list[str]) -> None:
        """Called when user manually edits a date — discard the relative token."""
        token_holder[0] = ""

    # --------------------------------------------------------------- page defaults
    def _apply_page_default(self, pd_saved: PageFilterDefault) -> None:
        """Apply a saved per-page default, resolving relative dates fresh."""
        sw = self._cfg.fiscal.six_week_january_years if self._cfg else []

        if pd_saved.start_relative:
            self._start_token[0] = pd_saved.start_relative
            d = resolve_relative_date(pd_saved.start_relative, sw)
            self.start_edit.blockSignals(True)
            self.start_edit.setDate(QDate(d.year, d.month, d.day))
            self.start_edit.blockSignals(False)
        elif pd_saved.start_iso:
            try:
                d = date.fromisoformat(pd_saved.start_iso)
                self.start_edit.setDate(QDate(d.year, d.month, d.day))
            except ValueError:
                pass

        if pd_saved.end_relative:
            self._end_token[0] = pd_saved.end_relative
            d = resolve_relative_date(pd_saved.end_relative, sw)
            self.end_edit.blockSignals(True)
            self.end_edit.setDate(QDate(d.year, d.month, d.day))
            self.end_edit.blockSignals(False)
        elif pd_saved.end_iso:
            try:
                d = date.fromisoformat(pd_saved.end_iso)
                self.end_edit.setDate(QDate(d.year, d.month, d.day))
            except ValueError:
                pass

        if pd_saved.cost_centers:
            self.cc.set_selected_codes(pd_saved.cost_centers)

        self.compare_prior.setChecked(pd_saved.vs_prior_year)

    def _save_page_default(self) -> None:
        """Persist the current filter state as the default for this page."""
        if not self._page_id or self._cfg is None:
            return
        s, e = self.date_range()
        ccs = self.selected_codes()
        pf = PageFilterDefault(
            start_relative=self._start_token[0],
            end_relative=self._end_token[0],
            start_iso="" if self._start_token[0] else s.isoformat(),
            end_iso="" if self._end_token[0] else e.isoformat(),
            cost_centers=ccs,
            vs_prior_year=self.compare_prior.isChecked(),
        )
        self._cfg.page_defaults[self._page_id] = pf
        try:
            save_config(self._cfg)
            if hasattr(self, "_saved_label"):
                start_desc = (
                    self._token_label(self._start_token[0])
                    if self._start_token[0] else s.strftime("%b %d, %Y")
                )
                end_desc = (
                    self._token_label(self._end_token[0])
                    if self._end_token[0] else e.strftime("%b %d, %Y")
                )
                self._saved_label.setText(f"✓ Saved: {start_desc} → {end_desc}")
        except Exception as exc:  # noqa: BLE001
            if hasattr(self, "_saved_label"):
                self._saved_label.setText(f"Save failed: {exc}")

    @staticmethod
    def _token_label(token: str) -> str:
        for tok, lbl in _RELATIVE_OPTIONS:
            if tok == token:
                return lbl
        return token

    # --------------------------------------------------------------- run
    def _on_cc_loaded(self, _count: int) -> None:
        if self._autorun and not self._has_autorun:
            self._has_autorun = True
            self._run(force_refresh=False)

    def _run(self, *, force_refresh: bool = False) -> None:
        # Re-resolve relative tokens every run so "yesterday" always means yesterday
        sw = self._cfg.fiscal.six_week_january_years if self._cfg else []
        if self._start_token[0]:
            d = resolve_relative_date(self._start_token[0], sw)
            self.start_edit.blockSignals(True)
            self.start_edit.setDate(QDate(d.year, d.month, d.day))
            self.start_edit.blockSignals(False)
        if self._end_token[0]:
            d = resolve_relative_date(self._end_token[0], sw)
            self.end_edit.blockSignals(True)
            self.end_edit.setDate(QDate(d.year, d.month, d.day))
            self.end_edit.blockSignals(False)

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
                self.busy_state_changed.emit("done")
                self.sales_loaded.emit(cur_df)
                self.sales_loaded_with_prior.emit(cur_df, prior_df)
                return

        scope = ", ".join(ccs) if ccs else "all CCs"
        self.status.setText(f"Loading {s} → {e} for {scope}…")
        self.run_btn.setEnabled(False)
        self.busy_state_changed.emit("loading")
        self.run_requested.emit(s, e, ccs)

        sw = list(self._cfg.fiscal.six_week_january_years) if self._cfg else []
        loader = _SalesLoader(
            self._get_db(), s, e, ccs, prior_start, prior_end, sw,
            self._code_prefix_filter,
        )
        loader.loaded.connect(self._on_loaded)
        loader.failed.connect(self._on_failed)
        # Hold a strong ref until ``finished`` (PySide6 will native-crash
        # if a running QThread is garbage-collected).
        self._loaders.append(loader)
        loader.finished.connect(lambda L=loader: self._loaders.remove(L) if L in self._loaders else None)
        loader.start()

    def _on_loaded(self, df: pd.DataFrame, prior: pd.DataFrame | None) -> None:
        self.run_btn.setEnabled(True)
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
        self.busy_state_changed.emit("done")
        self.sales_loaded.emit(df)
        self.sales_loaded_with_prior.emit(df, prior)

    def _on_failed(self, msg: str) -> None:
        self.run_btn.setEnabled(True)
        self.status.setText(f"Failed — {msg}")
        self.busy_state_changed.emit("failed")
        self.failed.emit(msg)

    def _refresh_cache_label(self) -> None:
        if self._last_cache_ts is None:
            self.cache_label.setText("")
            return
        self.cache_label.setText(
            f"Last refreshed {self._last_cache_ts.strftime('%b %d, %Y at %I:%M %p').lstrip('0')}"
        )

    # Called by the global “Refresh” button on the Dashboard. Caches have
    # already been cleared by that point; we just re-fire the loader.
    def refresh_data(self) -> None:
        self._last_cache_ts = None
        self._refresh_cache_label()
        self._run(force_refresh=True)

    def apply_filters(
        self,
        start: date | None,
        end: date | None,
        cost_centers: list[str] | None,
        *,
        run: bool = True,
    ) -> None:
        """Programmatically set this bar's filters and (by default) re-run.

        Used by the Dashboard's "Apply to all pages" action so a single
        global filter change updates every screen at once.
        Clears relative tokens — absolute dates are being imposed externally.
        """
        if start is not None and end is not None and end >= start:
            self._start_token[0] = ""
            self._end_token[0] = ""
            self._set_dates(start, end)
        if cost_centers is not None:
            self.cc.set_selected_codes(cost_centers)
        if run:
            self._run(force_refresh=False)
