"""Main application window: sidebar + stacked views + status bar."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QWidget,
)

from app import __app_name__
from app.ai.factory import build_provider
from app.config.models import AppConfig
from app.config.store import save_config
from app.data.db import ping as db_ping
from app.notifications.email_client import EmailClient
from app.storage import sales_cache, invoice_cache
from app.ui.dialogs.ai_settings_dialog import AISettingsDialog
from app.ui.dialogs.db_settings_dialog import DatabaseSettingsDialog
from app.ui.dialogs.email_settings_dialog import EmailSettingsDialog
from app.ui.views.ai_chat_view import AIChatView
from app.ui.views.cc_mapping_view import CCMappingView
from app.ui.views.conversations_view import ConversationsView
from app.ui.views.core_displays_view import CoreDisplaysView
from app.ui.views.dashboard_view import DashboardView
from app.ui.views.fiscal_calendar_view import FiscalCalendarView
from app.ui.views.reps_view import RepsView
from app.ui.views.sales_by_cc_view import SalesByCostCenterView
from app.ui.views.sales_by_rep_view import SalesByRepView
from app.ui.views.settings_view import SettingsView
from app.ui.views.weekly_email_view import WeeklyEmailView
from app.ui.widgets.sidebar import Sidebar
from app.ui.widgets.status_bar import AppStatusBar


NAV_ITEMS = [
    ("dashboard",     "Dashboard"),
    ("reps",          "Sales Reps"),
    ("sales_by_rep",  "Sales by Rep"),
    ("sales_by_cc",   "Sales by Cost Center"),
    ("conversations", "Conversations"),
    ("ai_chat",       "Ask the AI"),
    ("weekly_email",  "Weekly Email"),
    ("cc_mapping",    "CC Mapping"),
    ("core_displays", "Core Displays"),
    ("fiscal",        "Fiscal Calendar"),
    ("settings",      "Settings"),
]


class _StatusChecker(QThread):
    """Background liveness checks so the UI never freezes."""

    db_result = Signal(bool, str)
    email_result = Signal(bool, str)
    ai_result = Signal(bool, str)

    def __init__(self, cfg: AppConfig, *, check_email: bool, check_ai: bool) -> None:
        super().__init__()
        self._cfg = cfg
        self._check_email = check_email
        self._check_ai = check_ai

    def run(self) -> None:  # noqa: D401
        ok, msg = db_ping(self._cfg.database)
        self.db_result.emit(ok, msg)

        if self._check_email and self._cfg.email.smtp_host and self._cfg.email.smtp_username:
            ok, msg = EmailClient(self._cfg.email).test_smtp()
            self.email_result.emit(ok, msg)
        else:
            self.email_result.emit(False, "not configured")

        if self._check_ai and self._cfg.ai.api_username:
            try:
                ok, msg = build_provider(self._cfg.ai).ping()
            except Exception as exc:  # noqa: BLE001
                ok, msg = False, f"{type(exc).__name__}: {exc}"
            self.ai_result.emit(ok, msg)
        else:
            self.ai_result.emit(False, "not configured")


class MainWindow(QMainWindow):
    def __init__(self, cfg: AppConfig) -> None:
        super().__init__()
        self._cfg = cfg
        self.setWindowTitle(__app_name__)
        self.resize(QSize(1280, 820))
        self.setMinimumSize(QSize(1024, 680))

        # Root layout
        root = QWidget()
        root.setObjectName("contentRoot")
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        self.sidebar = Sidebar(NAV_ITEMS)
        self.sidebar.navigated.connect(self._navigate)

        self.stack = QStackedWidget()
        self.dashboard_view = DashboardView(self._cfg, get_db=lambda: self._cfg.database)
        self.reps_view = RepsView(get_db=lambda: self._cfg.database)
        self.sales_by_rep_view = SalesByRepView(self._cfg, get_db=lambda: self._cfg.database)
        self.sales_by_cc_view = SalesByCostCenterView(self._cfg, get_db=lambda: self._cfg.database)
        self.conversations_view = ConversationsView()
        self.ai_chat_view = AIChatView(self._cfg, get_db=lambda: self._cfg.database)
        self.weekly_email_view = WeeklyEmailView(self._cfg, get_db=lambda: self._cfg.database)
        self.cc_mapping_view = CCMappingView(self._cfg, get_db=lambda: self._cfg.database)
        self.core_displays_view = CoreDisplaysView(self._cfg, get_db=lambda: self._cfg.database)
        self.fiscal_view = FiscalCalendarView(self._cfg)
        self.settings_view = SettingsView()
        self.settings_view.open_db.connect(self._open_db_dialog)
        self.settings_view.open_email.connect(self._open_email_dialog)
        self.settings_view.open_ai.connect(self._open_ai_dialog)

        self._views: dict[str, QWidget] = {
            "dashboard":     self.dashboard_view,
            "reps":          self.reps_view,
            "sales_by_rep":  self.sales_by_rep_view,
            "sales_by_cc":   self.sales_by_cc_view,
            "conversations": self.conversations_view,
            "ai_chat":       self.ai_chat_view,
            "weekly_email":  self.weekly_email_view,
            "cc_mapping":    self.cc_mapping_view,
            "core_displays": self.core_displays_view,
            "fiscal":        self.fiscal_view,
            "settings":      self.settings_view,
        }
        for w in self._views.values():
            self.stack.addWidget(w)

        h.addWidget(self.sidebar)
        h.addWidget(self.stack, 1)

        self.setCentralWidget(root)

        self.status = AppStatusBar(self)
        self.setStatusBar(self.status)

        # Default view
        self.sidebar.select("dashboard")
        self._navigate("dashboard")

        # Kick off non-blocking liveness checks
        self._checker: _StatusChecker | None = None
        self.refresh_status_indicators()

        # Once the window is up, ask whether to refresh stale cached data.
        QTimer.singleShot(250, self._maybe_prompt_refresh)

    def _maybe_prompt_refresh(self) -> None:
        ts = sales_cache.latest_refresh()
        if ts is None:
            return
        when = ts.strftime("%B %d, %Y at %I:%M %p").replace(" 0", " ")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Refresh sales data?")
        box.setText(
            f"Cached sales data is available from <b>{when}</b>.<br><br>"
            "Use the cached data for an instant load, or refresh from the "
            "warehouse now (slower)?"
        )
        use_cached = box.addButton("Use cached", QMessageBox.ButtonRole.AcceptRole)
        refresh = box.addButton("Refresh from DB", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(use_cached)
        box.exec()
        if box.clickedButton() is refresh:
            sales_cache.clear_all()
            invoice_cache.clear_all()

    # ------------------------------------------------------------ navigation
    def _navigate(self, key: str) -> None:
        widget = self._views.get(key)
        if widget is not None:
            self.stack.setCurrentWidget(widget)
            self.sidebar.select(key)

    # ------------------------------------------------------------ dialogs
    def _open_db_dialog(self) -> None:
        dlg = DatabaseSettingsDialog(self._cfg.database, parent=self)
        if dlg.exec():
            self._cfg.database = dlg.result_config()
            save_config(self._cfg)
            self.refresh_status_indicators()

    def _open_email_dialog(self) -> None:
        dlg = EmailSettingsDialog(self._cfg.email, parent=self)
        if dlg.exec():
            dlg.commit_secrets()
            self._cfg.email = dlg.result_config()
            save_config(self._cfg)
            self.refresh_status_indicators()

    def _open_ai_dialog(self) -> None:
        dlg = AISettingsDialog(self._cfg.ai, parent=self)
        if dlg.exec():
            dlg.commit_secrets()
            self._cfg.ai = dlg.result_config()
            save_config(self._cfg)
            self.refresh_status_indicators()

    # ------------------------------------------------------------ status
    def refresh_status_indicators(self) -> None:
        self.status.db_indicator.set_state("unknown", "checking…")
        self.status.email_indicator.set_state("unknown", "checking…")
        self.status.ai_indicator.set_state("unknown", "checking…")

        check_email = bool(self._cfg.email.smtp_host and self._cfg.email.smtp_username)
        check_ai = bool(self._cfg.ai.api_username)

        self._checker = _StatusChecker(self._cfg, check_email=check_email, check_ai=check_ai)
        self._checker.db_result.connect(
            lambda ok, msg: self.status.db_indicator.set_state(
                "ok" if ok else "error", msg if ok else "disconnected"
            )
        )
        self._checker.email_result.connect(
            lambda ok, msg: self.status.email_indicator.set_state(
                "ok" if ok else ("warn" if "not configured" in msg else "error"),
                "ready" if ok else msg,
            )
        )
        self._checker.ai_result.connect(
            lambda ok, msg: self.status.ai_indicator.set_state(
                "ok" if ok else ("warn" if "not configured" in msg else "error"),
                "ready" if ok else msg,
            )
        )
        self._checker.start()
