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
from app.services.fiscal_calendar import (
    fiscal_year_for,
    fy_start_date,
    last_full_period,
)
from app.storage import invoice_cache, sales_cache
from app.ui.views._header import ViewHeader
from app.ui.widgets.cards import KpiCard
from app.ui.widgets.global_filters_card import GlobalFiltersCard


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

            # Fiscal YTD: from start of current FY → today.
            fy = fiscal_year_for(today)
            fy_start = fy_start_date(fy, sw)

            # Global default filters scope every blended call so KPIs
            # respond to the manager's preferred view of the business.
            d = self._cfg.defaults
            ccs_default = list(d.cost_centers) if d.cost_centers else None

            # Resolve the "Selected range" KPI window (or fall back to
            # rolling-year if defaults are unset).
            sel_start = sel_end = None
            if d.start_iso and d.end_iso:
                try:
                    sel_start = date.fromisoformat(d.start_iso)
                    sel_end = date.fromisoformat(d.end_iso)
                except ValueError:
                    sel_start = sel_end = None

            lfm_df = load_blended_sales(self._db, lfm.start, lfm.end, ccs_default, sw, "0")
            ytd_df = load_blended_sales(self._db, fy_start, today, ccs_default, sw, "0")

            sel_revenue = None
            sel_label = ""
            if sel_start is not None and sel_end is not None:
                sel_df = load_blended_sales(self._db, sel_start, sel_end, ccs_default, sw, "0")
                sel_revenue = float(sel_df["revenue"].sum() or 0) if sel_df is not None and not sel_df.empty else 0.0
                sel_label = f"{sel_start.isoformat()} → {sel_end.isoformat()}"

            try:
                open_df = load_open_orders(self._db, ccs_default, "0")
            except Exception:  # noqa: BLE001
                open_df = None

            recent_reps = 0
            try:
                from app.data.loaders import load_invoiced_sales
                ninety_days_ago = today - timedelta(days=90)
                recent = load_invoiced_sales(
                    self._db, ninety_days_ago, today, ccs_default, "0"
                )
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
                "ytd_label": f"FY{fy} YTD · since {fy_start.isoformat()}",
                "open_orders": float(open_df["open_revenue"].sum() or 0) if open_df is not None and not open_df.empty else 0.0,
                "open_lines": int(len(open_df)) if open_df is not None else 0,
                "active_reps": int(recent_reps),
                "sel_revenue": sel_revenue,
                "sel_label": sel_label,
            })
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class DashboardView(QWidget):
    refresh_all_requested = Signal()
    apply_global_filters_requested = Signal(object, object, list)  # start, end, ccs
    save_global_filters_requested = Signal(object, object, list, bool)  # +vs_prior
    busy_state_changed = Signal(str)  # "loading" | "done" | "failed"

    def __init__(
        self,
        cfg: AppConfig | None = None,
        get_db: Callable[[], DatabaseConfig] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._get_db = get_db
        self._loaders: list[_DashboardLoader] = []

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
            "ytd_revenue": KpiCard("Fiscal year-to-date", "—", "Revenue (blended)"),
            "sel_revenue": KpiCard("Selected range", "—", "Default-filter revenue"),
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

        # Global filter card — only when wired to config + DB.
        if cfg is not None and get_db is not None:
            self.global_filters = GlobalFiltersCard(cfg, get_db)
            self.global_filters.apply_requested.connect(
                self.apply_global_filters_requested.emit
            )
            self.global_filters.save_requested.connect(
                self.save_global_filters_requested.emit
            )
            root.addWidget(self.global_filters)
        else:
            self.global_filters = None  # type: ignore[assignment]

        root.addStretch(1)

        if self._get_db is not None and self._cfg is not None:
            QTimer.singleShot(0, self._reload)

    def _reload(self) -> None:
        if self._get_db is None or self._cfg is None:
            return
        self.refresh_btn.setEnabled(False)
        for k in ("lfm_revenue", "ytd_revenue", "sel_revenue", "open_orders", "active_reps"):
            self.cards[k].set_value("…")
        self.busy_state_changed.emit("loading")
        loader = _DashboardLoader(self._get_db(), self._cfg)
        loader.loaded.connect(self._on_loaded)
        loader.failed.connect(self._on_failed)
        self._loaders.append(loader)
        loader.finished.connect(
            lambda L=loader: self._loaders.remove(L) if L in self._loaders else None
        )
        loader.start()

    def _on_loaded(self, data: dict) -> None:
        self.refresh_btn.setEnabled(True)
        self.cards["lfm_revenue"].set_value(f"${data['lfm_revenue']:,.0f}")
        self.cards["lfm_revenue"].set_caption(data.get("lfm_label", ""))
        self.cards["ytd_revenue"].set_value(f"${data['ytd_revenue']:,.0f}")
        self.cards["ytd_revenue"].set_caption(data.get("ytd_label", ""))
        sel = data.get("sel_revenue")
        if sel is None:
            self.cards["sel_revenue"].set_value("—")
            self.cards["sel_revenue"].set_caption("No default range set")
        else:
            self.cards["sel_revenue"].set_value(f"${sel:,.0f}")
            self.cards["sel_revenue"].set_caption(data.get("sel_label", ""))
        self.cards["open_orders"].set_value(f"${data['open_orders']:,.0f}")
        self.cards["open_orders"].set_caption(
            f"{data.get('open_lines', 0):,} un-invoiced lines"
        )
        self.cards["active_reps"].set_value(f"{data['active_reps']:,}")
        self.busy_state_changed.emit("done")

    def _on_failed(self, msg: str) -> None:
        self.refresh_btn.setEnabled(True)
        for k in ("lfm_revenue", "ytd_revenue", "sel_revenue", "open_orders", "active_reps"):
            self.cards[k].set_value("—")
            self.cards[k].set_caption(f"Failed: {msg}")
        self.busy_state_changed.emit("failed")

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
