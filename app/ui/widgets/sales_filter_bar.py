"""Shared filter bar: cost-center selector + date range + Run button.

Used by both the Sales-by-Rep and Sales-by-Cost-Center views.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

from PySide6.QtCore import QDate, QThread, Signal
from PySide6.QtWidgets import (
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

from app.config.models import DatabaseConfig
from app.data.loaders import load_invoiced_sales
from app.ui.theme import TEXT_MUTED
from app.ui.widgets.cc_selector import CostCenterSelector


class _SalesLoader(QThread):
    loaded = Signal(object)  # pd.DataFrame
    failed = Signal(str)

    def __init__(self, db: DatabaseConfig, start: date, end: date, ccs: list[str]) -> None:
        super().__init__()
        self._db, self._start, self._end, self._ccs = db, start, end, ccs

    def run(self) -> None:  # noqa: D401
        try:
            df = load_invoiced_sales(self._db, self._start, self._end, self._ccs or None)
            self.loaded.emit(df)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class SalesFilterBar(QFrame):
    """Sidebar-style filter card with CC selector + date inputs + Run button."""

    run_requested = Signal(object, object, list)  # start: date, end: date, cc_codes
    sales_loaded = Signal(object)                  # pd.DataFrame
    failed = Signal(str)

    def __init__(self, get_db: Callable[[], DatabaseConfig], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setMinimumWidth(320)
        self.setMaximumWidth(360)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._get_db = get_db
        self._loader: _SalesLoader | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        self.cc = CostCenterSelector(get_db)
        root.addWidget(self.cc, 1)

        # Date range
        date_label = QLabel("Date Range (invoice date)")
        date_label.setStyleSheet("font-weight: 600;")
        root.addWidget(date_label)

        today = QDate.currentDate()
        self.start_edit = QDateEdit()
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDate(today.addDays(-30))
        self.end_edit = QDateEdit()
        self.end_edit.setCalendarPopup(True)
        self.end_edit.setDate(today)

        d_row1 = QHBoxLayout()
        d_row1.addWidget(QLabel("From"))
        d_row1.addWidget(self.start_edit, 1)
        root.addLayout(d_row1)
        d_row2 = QHBoxLayout()
        d_row2.addWidget(QLabel("To  "))
        d_row2.addWidget(self.end_edit, 1)
        root.addLayout(d_row2)

        # Quick date presets
        presets = QHBoxLayout()
        for label, days in (("7d", 7), ("30d", 30), ("90d", 90), ("YTD", -1)):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, d=days: self._apply_preset(d))
            presets.addWidget(b)
        root.addLayout(presets)

        self.status = QLabel("Pick cost centers + dates and press Run.")
        self.status.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self.run_btn = QPushButton("Run")
        self.run_btn.setProperty("primary", True)
        self.run_btn.clicked.connect(self._run)
        root.addWidget(self.run_btn)

    # --------------------------------------------------------------- public
    def selected_codes(self) -> list[str]:
        return self.cc.selected_codes()

    def date_range(self) -> tuple[date, date]:
        s = self.start_edit.date().toPython()
        e = self.end_edit.date().toPython()
        return s, e

    def reload_cost_centers(self) -> None:
        self.cc.reload()

    # --------------------------------------------------------------- internal
    def _apply_preset(self, days: int) -> None:
        today = QDate.currentDate()
        if days == -1:  # YTD (calendar)
            self.start_edit.setDate(QDate(today.year(), 1, 1))
        else:
            self.start_edit.setDate(today.addDays(-days))
        self.end_edit.setDate(today)

    def _run(self) -> None:
        ccs = self.selected_codes()
        s, e = self.date_range()
        if e < s:
            self.status.setText("End date must be on/after start date.")
            return
        self.status.setText(f"Loading invoiced sales {s} → {e} for {len(ccs) or 'all'} CC(s)…")
        self.run_btn.setEnabled(False)
        self.run_requested.emit(s, e, ccs)
        self._loader = _SalesLoader(self._get_db(), s, e, ccs)
        self._loader.loaded.connect(self._on_loaded)
        self._loader.failed.connect(self._on_failed)
        self._loader.start()

    def _on_loaded(self, df: pd.DataFrame) -> None:
        self.run_btn.setEnabled(True)
        self.status.setText(f"Loaded {len(df):,} invoiced lines.")
        self.sales_loaded.emit(df)

    def _on_failed(self, msg: str) -> None:
        self.run_btn.setEnabled(True)
        self.status.setText(f"Failed — {msg}")
        self.failed.emit(msg)
