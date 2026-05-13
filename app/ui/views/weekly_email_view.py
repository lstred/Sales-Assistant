"""Weekly Sales Email composer.

For the selected cost centers and date range, generates one personalized
draft per rep showing their sales for the period. Drafts are reviewed in
this screen and (later) sent through :mod:`app.notifications.email_client`
when outbound is enabled.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.config.models import AppConfig, DatabaseConfig
from app.ui.theme import ACCENT, BORDER, SURFACE, TEXT, TEXT_MUTED
from app.ui.views._header import ViewHeader
from app.ui.widgets.sales_filter_bar import SalesFilterBar


class WeeklyEmailView(QWidget):
    def __init__(self, cfg: AppConfig, get_db: Callable[[], DatabaseConfig], parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._df: pd.DataFrame | None = None
        self._drafts: dict[str, dict] = {}  # rep_key -> {subject, body_html, ...}

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)

        root.addWidget(
            ViewHeader(
                "Weekly Sales Email",
                "One draft per rep summarising their sales for the period and "
                "selected cost centers. Outbound sending stays disabled until "
                "you flip it on in Email settings.",
            )
        )

        body = QHBoxLayout()
        body.setSpacing(12)
        self.filter_bar = SalesFilterBar(get_db)
        self.filter_bar.sales_loaded.connect(self._on_loaded)
        body.addWidget(self.filter_bar)

        # Drafts area
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(8)

        actions = QHBoxLayout()
        self.gen_btn = QPushButton("Generate drafts")
        self.gen_btn.setProperty("primary", True)
        self.gen_btn.setEnabled(False)
        self.gen_btn.clicked.connect(self._generate)
        actions.addWidget(self.gen_btn)
        self.queue_btn = QPushButton("Queue for review")
        self.queue_btn.setEnabled(False)
        self.queue_btn.clicked.connect(self._queue)
        actions.addWidget(self.queue_btn)
        actions.addStretch(1)
        rv.addLayout(actions)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.list = QListWidget()
        self.list.itemSelectionChanged.connect(self._show_selected)
        self.list.setMinimumWidth(240)
        splitter.addWidget(self.list)

        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(False)
        self.preview.setStyleSheet(
            f"QTextBrowser {{ background: {SURFACE}; border: 1px solid {BORDER};"
            f" border-radius: 8px; padding: 14px; color: {TEXT}; }}"
        )
        splitter.addWidget(self.preview)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        rv.addWidget(splitter, 1)
        body.addWidget(right, 1)
        root.addLayout(body, 1)

    # --------------------------------------------------------------- data
    def _on_loaded(self, df: pd.DataFrame) -> None:
        self._df = df
        self.gen_btn.setEnabled(not df.empty)
        self.queue_btn.setEnabled(False)
        self.list.clear()
        self.preview.clear()
        self.preview.setPlainText(
            "" if df.empty else f"Loaded {len(df):,} invoiced lines. Press "
                                "Generate drafts to compose one email per rep."
        )

    def _generate(self) -> None:
        if self._df is None or self._df.empty:
            return
        s, e = self.filter_bar.date_range()
        ccs = self.filter_bar.selected_codes()
        cc_label = ", ".join(ccs) if ccs else "all cost centers"

        self._drafts.clear()
        self.list.clear()

        df = self._df.copy()
        df["rep_key"] = df["salesperson_number"].fillna("").astype(str).str.strip()
        df["rep_name"] = df["salesperson_desc"].fillna("").astype(str).str.strip()
        # Group by rep
        for rep_key, rep_df in df.groupby("rep_key"):
            if not rep_key:
                continue
            rep_name = rep_df["rep_name"].iloc[0] or rep_key
            rev = float(rep_df["revenue"].sum() or 0)
            gp = float(rep_df["gross_profit"].sum() or 0)
            gpp = (gp / rev * 100) if rev else 0
            lines = int(len(rep_df))
            top_accounts = (
                rep_df.groupby("account_number", as_index=False)
                      .agg(revenue=("revenue", "sum"))
                      .sort_values("revenue", ascending=False)
                      .head(5)
            )
            by_cc = (
                rep_df.groupby("cost_center", as_index=False)
                      .agg(revenue=("revenue", "sum"))
                      .sort_values("revenue", ascending=False)
            )
            email_to = self._cfg.rep_emails.get(rep_key, "")
            subject = f"Your sales recap · {s.isoformat()} – {e.isoformat()}"
            body_html = _render_html(
                rep_name=rep_name,
                rep_key=rep_key,
                start=s, end=e,
                cc_label=cc_label,
                revenue=rev, gp=gp, gpp=gpp, lines=lines,
                top_accounts=top_accounts,
                by_cc=by_cc,
            )
            self._drafts[rep_key] = {
                "to": email_to,
                "subject": subject,
                "body_html": body_html,
                "rep_name": rep_name,
            }
            label = f"{rep_name}  ·  ${rev:,.0f}"
            if not email_to:
                label += "   (no email on file)"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, rep_key)
            self.list.addItem(item)

        self.queue_btn.setEnabled(bool(self._drafts))
        if self.list.count():
            self.list.setCurrentRow(0)

    def _show_selected(self) -> None:
        items = self.list.selectedItems()
        if not items:
            return
        key = items[0].data(Qt.ItemDataRole.UserRole)
        d = self._drafts.get(key)
        if not d:
            return
        header = (
            f"<div style='color:{TEXT_MUTED};font-size:12px;margin-bottom:8px;'>"
            f"<b>To:</b> {d['to'] or '(no email on file)'}<br>"
            f"<b>Subject:</b> {d['subject']}</div>"
        )
        self.preview.setHtml(header + d["body_html"])

    def _queue(self) -> None:
        # Persisting drafts to local SQLite "send_log" lands with the AI
        # composition pipeline; for now this just confirms what would queue.
        ready = sum(1 for d in self._drafts.values() if d["to"])
        missing = len(self._drafts) - ready
        self.preview.setHtml(
            f"<h3>Ready to queue</h3><p>{ready} draft(s) have a recipient. "
            f"{missing} draft(s) are missing an email address — set them in "
            f"Settings → Reps before sending.</p>"
        )


def _render_html(*, rep_name, rep_key, start, end, cc_label,
                 revenue, gp, gpp, lines, top_accounts, by_cc) -> str:
    rows_top = "".join(
        f"<tr><td>{r['account_number']}</td>"
        f"<td style='text-align:right'>${float(r['revenue']):,.0f}</td></tr>"
        for _, r in top_accounts.iterrows()
    ) or "<tr><td colspan='2' style='color:#888'>No invoiced sales.</td></tr>"
    rows_cc = "".join(
        f"<tr><td>{r['cost_center']}</td>"
        f"<td style='text-align:right'>${float(r['revenue']):,.0f}</td></tr>"
        for _, r in by_cc.iterrows()
    ) or "<tr><td colspan='2' style='color:#888'>—</td></tr>"
    return (
        f"<p>Hi {rep_name.split()[0] if rep_name else 'there'},</p>"
        f"<p>Here's your sales recap for <b>{start.isoformat()} → {end.isoformat()}</b> "
        f"covering {cc_label}.</p>"
        f"<h3 style='margin-bottom:6px;'>The numbers</h3>"
        f"<ul>"
        f"<li><b>Revenue:</b> ${revenue:,.0f}</li>"
        f"<li><b>Gross profit:</b> ${gp:,.0f} ({gpp:,.1f}%)</li>"
        f"<li><b>Invoice lines:</b> {lines:,}</li>"
        f"</ul>"
        f"<h3 style='margin-bottom:6px;'>Top accounts</h3>"
        f"<table cellpadding='6' style='border-collapse:collapse;'>{rows_top}</table>"
        f"<h3 style='margin-top:14px;margin-bottom:6px;'>By cost center</h3>"
        f"<table cellpadding='6' style='border-collapse:collapse;'>{rows_cc}</table>"
        f"<p style='color:#888;margin-top:14px;font-size:11px;'>"
        f"Salesman #{rep_key} · generated by Sales Assistant.</p>"
    )
