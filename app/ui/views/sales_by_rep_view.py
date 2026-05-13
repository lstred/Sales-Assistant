"""Sales by Sales Rep — invoiced sales pivoted by rep."""

from __future__ import annotations

import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.ui.views._header import ViewHeader
from app.ui.widgets.cards import KpiCard
from app.ui.widgets.pandas_model import PandasModel
from app.ui.widgets.sales_filter_bar import SalesFilterBar


class SalesByRepView(QWidget):
    def __init__(self, cfg=None, get_db=None, parent=None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)

        root.addWidget(
            ViewHeader(
                "Sales by Sales Rep",
                "Invoiced sales for the selected cost centers and date range, "
                "grouped by sales rep.",
            )
        )

        # KPI row
        kpi_row = QHBoxLayout()
        kpi_row.setSpacing(12)
        self.kpi_revenue = KpiCard("Revenue")
        self.kpi_gp = KpiCard("Gross Profit")
        self.kpi_gpp = KpiCard("GP %")
        self.kpi_lines = KpiCard("Invoice Lines")
        for k in (self.kpi_revenue, self.kpi_gp, self.kpi_gpp, self.kpi_lines):
            kpi_row.addWidget(k, 1)
        root.addLayout(kpi_row)

        body = QHBoxLayout()
        body.setSpacing(12)
        self.filter_bar = SalesFilterBar(get_db, cfg=cfg)
        self.filter_bar.sales_loaded.connect(self._on_loaded)
        body.addWidget(self.filter_bar)

        self.model = PandasModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        body.addWidget(self.table, 1)
        root.addLayout(body, 1)

    def _on_loaded(self, df: pd.DataFrame) -> None:
        if df is None or df.empty:
            self.model.set_dataframe(pd.DataFrame())
            self._set_kpis(0, 0, 0, 0)
            return
        rep = (
            df.assign(rep_label=lambda d: d["salesperson_desc"].fillna("(unassigned)").replace("", "(unassigned)"))
              .groupby("rep_label", as_index=False)
              .agg(revenue=("revenue", "sum"),
                   gross_profit=("gross_profit", "sum"),
                   invoice_lines=("invoice_number", "count"),
                   accounts=("account_number", pd.Series.nunique))
              .sort_values("revenue", ascending=False)
        )
        rep["gp_pct"] = (rep["gross_profit"] / rep["revenue"] * 100).round(1)
        rep = rep[["rep_label", "revenue", "gross_profit", "gp_pct", "invoice_lines", "accounts"]]
        self.model.set_dataframe(rep)
        self._set_kpis(
            float(df["revenue"].sum() or 0),
            float(df["gross_profit"].sum() or 0),
            float((df["gross_profit"].sum() / df["revenue"].sum() * 100) if df["revenue"].sum() else 0),
            int(len(df)),
        )

    def _set_kpis(self, rev: float, gp: float, gpp: float, lines: int) -> None:
        self.kpi_revenue.set_value(f"${rev:,.0f}")
        self.kpi_gp.set_value(f"${gp:,.0f}")
        self.kpi_gpp.set_value(f"{gpp:,.1f}%")
        self.kpi_lines.set_value(f"{lines:,}")
