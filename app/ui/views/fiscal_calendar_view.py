"""Fiscal Calendar setup view.

Shows the computed 4-4-5 calendar for any fiscal year and lets the user
flag rare 6-week-January overrides. Anchored to FY 2027 starting
Sunday Feb 1 2026 — see :mod:`app.services.fiscal_calendar`.
"""

from __future__ import annotations

import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.config.models import AppConfig
from app.config.store import save_config
from app.services.fiscal_calendar import (
    FY_ANCHOR_DATE,
    FY_ANCHOR_LABEL,
    build_fiscal_year,
    fiscal_year_for,
)
from app.ui.theme import TEXT_MUTED
from app.ui.views._header import ViewHeader
from app.ui.widgets.cards import KpiCard
from app.ui.widgets.pandas_model import PandasModel
from datetime import date


class FiscalCalendarView(QWidget):
    def __init__(self, cfg: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)

        root.addWidget(
            ViewHeader(
                "Fiscal Calendar",
                "NRF fiscal months follow a 4-4-5 week pattern. Every month starts "
                "on a Sunday. FY 2027 anchor: Sunday February 1, 2026. "
                "Flag January here if it needs to be 6 weeks to realign.",
            )
        )

        # KPI summary
        kpi_row = QHBoxLayout()
        self.kpi_anchor = KpiCard("FY27 Anchor", FY_ANCHOR_DATE.strftime("%a, %b %d %Y"))
        self.kpi_today = KpiCard("Today's Period")
        self.kpi_year_weeks = KpiCard("Weeks in FY")
        for k in (self.kpi_anchor, self.kpi_today, self.kpi_year_weeks):
            kpi_row.addWidget(k, 1)
        root.addLayout(kpi_row)

        # Year selector + 6-week-January toggle
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Fiscal year"))
        self.year_spin = QSpinBox()
        self.year_spin.setRange(2000, 2099)
        current_fy = fiscal_year_for(date.today())
        self.year_spin.setValue(current_fy)
        self.year_spin.valueChanged.connect(self._refresh)
        controls.addWidget(self.year_spin)

        self.six_week_jan = QCheckBox("January is 6 weeks this fiscal year")
        self.six_week_jan.toggled.connect(self._toggle_six_week)
        controls.addWidget(self.six_week_jan)
        controls.addStretch(1)

        self.help = QLabel("(Hint: a 6-week January realigns the FY back to the calendar year — rare.)")
        self.help.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        controls.addWidget(self.help)
        root.addLayout(controls)

        self.model = PandasModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table, 1)

        self._refresh()

    def _refresh(self) -> None:
        fy = self.year_spin.value()
        self.six_week_jan.blockSignals(True)
        self.six_week_jan.setChecked(fy in self._cfg.fiscal.six_week_january_years)
        self.six_week_jan.blockSignals(False)

        sw = self._cfg.fiscal.six_week_january_years
        periods = build_fiscal_year(fy, sw)
        df = pd.DataFrame([
            {
                "period": p.period,
                "month": p.name,
                "weeks": p.weeks,
                "start": p.start.strftime("%a %Y-%m-%d"),
                "end":   p.end.strftime("%a %Y-%m-%d"),
                "days":  (p.end - p.start).days + 1,
            }
            for p in periods
        ])
        self.model.set_dataframe(df)

        total_weeks = sum(p.weeks for p in periods)
        self.kpi_year_weeks.set_value(f"{total_weeks}",
                                     f"{total_weeks * 7} days · {len(periods)} months")

        # Today's period
        try:
            today = date.today()
            from app.services.fiscal_calendar import find_period
            p = find_period(today, sw)
            self.kpi_today.set_value(
                f"FY{p.fiscal_year} P{p.period:02d}",
                f"{p.name} · week {((today - p.start).days // 7) + 1} of {p.weeks}",
            )
        except Exception as exc:  # noqa: BLE001
            self.kpi_today.set_value("—", str(exc))

    def _toggle_six_week(self, on: bool) -> None:
        fy = self.year_spin.value()
        years = set(self._cfg.fiscal.six_week_january_years)
        if on:
            years.add(fy)
        else:
            years.discard(fy)
        self._cfg.fiscal.six_week_january_years = sorted(years)
        save_config(self._cfg)
        self._refresh()
