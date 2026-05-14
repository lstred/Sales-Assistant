"""Dashboard — at-a-glance KPIs from the warehouse + local app state."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.config.models import AppConfig, DatabaseConfig
from app.data.loaders import load_blended_sales, load_open_orders
from app.services.fiscal_calendar import last_full_period
from app.storage import invoice_cache, sales_cache
from app.ui.views._header import ViewHeader
from app.ui.widgets.cards import KpiCard


class _DashboardLoader(QThread):
    loaded = Signal(dict)
    failed = Signal(str)

    def __init__(self, db: DatabaseConfig, cfg: AppConfig) -> None:
        super().__init__()
        self._db = db
        self._cfg = cfg

    def run(self) -> None:  # noqa: D401
        try:
            sw = list(self._cfg.fiscal.six_week_january_years)
            today = date.today()
            lfm = last_full_period(today, sw)
            ytd_start = date(today.year, 1, 1)

            lfm_df = load_blended_sales(self._db, lfm.start, lfm.end, None, sw)
            ytd_df = load_blended_sales(self._db, ytd_start, today, None, sw)
            try:
                open_df = load_open_orders(self._db, None)
            except Exception:  # noqa: BLE001
                open_df = None

            recent_reps = 0
            try:
                from app.data.loaders import load_invoiced_sales
                ninety_days_ago = today - timedelta(days=90)
                recent = load_invoiced_sales(self._db, ninety_days_ago, today, None)
                if recent is not None and not recent.empty:
                    recent_reps = (
                        recent["salesperson_desc"]
                        .fillna("").astype(str).str.strip()
                        .replace("", float("nan"))
                        .dropna()
                        .nunique()
                    )
            except Exception:  # noqa: BLE001
                recent_reps = 0

            self.loaded.emit({
                "lfm_label": f"FY{lfm.fiscal_year} P{lfm.period} · {lfm.name}",
                "lfm_revenue": float(lfm_df["revenue"].sum() or 0) if lfm_df is not None and not lfm_df.empty else 0.0,
                "ytd_revenue": float(ytd_df["revenue"].sum() or 0) if ytd_df is not None and not ytd_df.empty else 0.0,
                "ytd_label": f"YTD {today.year}",
                "open_orders": float(open_df["open_revenue"].sum() or 0) if open_df is not None and not open_df.empty else 0.0,
                "open_lines": int(len(open_df)) if open_df is not None else 0,
                "active_reps": int(recent_reps),
            })
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class DashboardView(QWidget):
    refresh_all_requested = Signal()

    def __init__(
        self,
        cfg: AppConfig | None = None,
        get_db: Callable[[], DatabaseConfig] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._get_db = get_db
        self._loader: _DashboardLoader | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(16)

        header_row = QHBoxLayout()
        header_row.addWidget(
            ViewHeader(
                "Overview",
                "Cross-territory snapshot of recent sales activity, "
                "pipeline, and outstanding follow-ups.",
            ),
            1,
        )
        self.refresh_btn = QPushButton("Refresh all data from database")
        self.refresh_btn.setProperty("primary", True)
        self.refresh_btn.setToolTip(
            "Wipes the local sales cache and re-fetches from SQL Server.\n"
            "Affects every screen in the app."
        )
        self.refresh_btn.clicked.connect(self._refresh_everything)
        header_row.addWidget(self.refresh_btn)
        root.addLayout(header_row)

        grid = QGridLayout()
        grid.setSpacing(16)
        self.cards = {
            "lfm_revenue": KpiCard("Last full fiscal month", "—", "Revenue (blended)"),
            "ytd_revenue": KpiCard("Year-to-date", "—", "Revenue (blended)"),
            "open_orders": KpiCard("Open orders", "—", "Un-invoiced pipeline"),
            "active_reps": KpiCard("Active reps", "—", "Distinct sellers · last 90d"),
        }
        for col, card in enumerate(self.cards.values()):
            grid.addWidget(card, 0, col)
        root.addLayout(grid)

        sub_grid = QGridLayout()
        sub_grid.setSpacing(16)
        self.cards["active_convos"] = KpiCard(
            "Active conversations", "0", "AI-managed email threads"
        )
        self.cards["open_actions"] = KpiCard(
            "Open action items", "0", "Commitments awaiting follow-up"
        )
        self.cards["needs_review"] = KpiCard(
            "Needs review", "0", "Drafts awaiting your approval"
        )
        sub_grid.addWidget(self.cards["active_convos"], 0, 0)
        sub_grid.addWidget(self.cards["open_actions"], 0, 1)
        sub_grid.addWidget(self.cards["needs_review"], 0, 2)
        root.addLayout(sub_grid)

        root.addStretch(1)

        if self._get_db is not None and self._cfg is not None:
            QTimer.singleShot(0, self._reload)

    def _reload(self) -> None:
        if self._get_db is None or self._cfg is None:
            return
        self.refresh_btn.setEnabled(False)
        for k in ("lfm_revenue", "ytd_revenue", "open_orders", "active_reps"):
            self.cards[k].set_value("…")
        self._loader = _DashboardLoader(self._get_db(), self._cfg)
        self._loader.loaded.connect(self._on_loaded)
        self._loader.failed.connect(self._on_failed)
        self._loader.start()

    def _on_loaded(self, data: dict) -> None:
        self.refresh_btn.setEnabled(True)
        self.cards["lfm_revenue"].set_value(f"${data['lfm_revenue']:,.0f}")
        self.cards["lfm_revenue"].set_caption(data.get("lfm_label", ""))
        self.cards["ytd_revenue"].set_value(f"${data['ytd_revenue']:,.0f}")
        self.cards["ytd_revenue"].set_caption(data.get("ytd_label", ""))
        self.cards["open_orders"].set_value(f"${data['open_orders']:,.0f}")
        self.cards["open_orders"].set_caption(
            f"{data.get('open_lines', 0):,} un-invoiced lines"
        )
        self.cards["active_reps"].set_value(f"{data['active_reps']:,}")

    def _on_failed(self, msg: str) -> None:
        self.refresh_btn.setEnabled(True)
        for k in ("lfm_revenue", "ytd_revenue", "open_orders", "active_reps"):
            self.cards[k].set_value("—")
            self.cards[k].set_caption(f"Failed: {msg}")

    # ----------------------------------------------------- global refresh
    def _refresh_everything(self) -> None:
        """Clear every local cache and re-warm. Triggers reload across
        every view via the ``refresh_all_requested`` signal which
        :class:`MainWindow` wires up to each data-driven view."""
        try:
            invoice_cache.clear_all()
            sales_cache.clear_all()
        except Exception:  # noqa: BLE001
            pass
        self._reload()
        self.refresh_all_requested.emit()
