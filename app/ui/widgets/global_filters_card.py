"""Compact "Default filters" card shown on the Dashboard.

Lets the manager set the app-wide default date range, cost-center selection,
and vs-prior-year toggle. Two actions:

* **Apply to all pages** — broadcasts the current values to every
  :class:`SalesFilterBar` in the app via a signal handled by
  :class:`MainWindow`. Pages instantly reload (cache hit if the same
  filters were used recently).
* **Save as default** — persists the current values into ``AppConfig.defaults``
  so they survive restart and every page opens already pre-filtered.
"""

from __future__ import annotations

from datetime import date
from typing import Callable

from PySide6.QtCore import QDate, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDateEdit,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from app.config.models import AppConfig, DatabaseConfig
from app.services.fiscal_calendar import last_n_full_periods_range
from app.ui.theme import TEXT_MUTED
from app.ui.widgets.cc_selector import CostCenterSelector


class GlobalFiltersCard(QFrame):
    """Self-contained card. Emits :pyattr:`apply_requested` and
    :pyattr:`save_requested` with the current values."""

    apply_requested = Signal(object, object, list)   # start, end, ccs
    save_requested = Signal(object, object, list, bool)  # start, end, ccs, vs_prior

    def __init__(
        self,
        cfg: AppConfig,
        get_db: Callable[[], DatabaseConfig],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._cfg = cfg
        self._get_db = get_db

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        title = QLabel("Default filters")
        title.setStyleSheet("font-weight: 700; font-size: 14px;")
        subtitle = QLabel(
            "Set the date range and cost centers used everywhere. "
            "Apply to refresh every page in sync."
        )
        subtitle.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(subtitle)

        # Resolve current defaults (or auto-fallback to last 12 fiscal periods).
        sw = list(cfg.fiscal.six_week_january_years)
        d = cfg.defaults
        start: date | None = None
        end: date | None = None
        if d.start_iso and d.end_iso:
            try:
                start = date.fromisoformat(d.start_iso)
                end = date.fromisoformat(d.end_iso)
            except ValueError:
                start = end = None
        if start is None or end is None:
            try:
                start, end = last_n_full_periods_range(date.today(), 12, sw)
            except Exception:  # noqa: BLE001
                today = date.today()
                start, end = today.replace(year=today.year - 1), today

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)

        grid.addWidget(QLabel("From"), 0, 0)
        self.start_edit = QDateEdit()
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDate(QDate(start.year, start.month, start.day))
        grid.addWidget(self.start_edit, 0, 1)

        grid.addWidget(QLabel("To"), 0, 2)
        self.end_edit = QDateEdit()
        self.end_edit.setCalendarPopup(True)
        self.end_edit.setDate(QDate(end.year, end.month, end.day))
        grid.addWidget(self.end_edit, 0, 3)

        self.compare_prior = QCheckBox("Compare vs prior year")
        self.compare_prior.setChecked(d.vs_prior_year)
        grid.addWidget(self.compare_prior, 0, 4)
        grid.setColumnStretch(5, 1)
        root.addLayout(grid)

        # Cost-center selector (compact — restrict to product CCs).
        self.cc = CostCenterSelector(
            get_db,
            autoload=True,
            select_all_after_load=True,
            code_prefix_filter="0",
            default_selected=list(d.cost_centers) if d.cost_centers else None,
        )
        self.cc.setMaximumHeight(180)
        root.addWidget(self.cc)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.apply_btn = QPushButton("Apply to all pages")
        self.apply_btn.setProperty("primary", True)
        self.apply_btn.setToolTip(
            "Update every page's filters to match these and reload."
        )
        self.save_btn = QPushButton("Save as default")
        self.save_btn.setToolTip(
            "Persist these filters so the app opens with them next launch."
        )
        btn_row.addWidget(self.apply_btn)
        btn_row.addWidget(self.save_btn)
        btn_row.addStretch(1)
        root.addLayout(btn_row)

        self.apply_btn.clicked.connect(self._on_apply)
        self.save_btn.clicked.connect(self._on_save)

    # ------------------------------------------------------------- internal
    def _values(self) -> tuple[date, date, list[str], bool]:
        s = self.start_edit.date().toPython()
        e = self.end_edit.date().toPython()
        ccs = self.cc.selected_codes()
        return s, e, ccs, self.compare_prior.isChecked()

    def _on_apply(self) -> None:
        s, e, ccs, _ = self._values()
        if e < s:
            return
        self.apply_requested.emit(s, e, ccs)

    def _on_save(self) -> None:
        s, e, ccs, vs_prior = self._values()
        if e < s:
            return
        self.save_requested.emit(s, e, ccs, vs_prior)
