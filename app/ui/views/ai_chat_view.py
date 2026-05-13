"""Ask the AI a question directly about the currently-loaded sales data.

Workflow:
1. User picks cost centers + date range (shared filter bar).
2. *Run* loads invoiced sales and refreshes the token estimate.
3. User types a question, presses *Ask*.
4. The view builds a compact CSV summary of the data, sends it as system
   context to the configured AI provider, and renders the reply.

Privacy note: this view is for the **manager** — it has no rep-scoping
restriction. Per-rep AI flows live in the Reps / Conversations views.
"""

from __future__ import annotations

from typing import Callable

import io
import pandas as pd
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.ai.base import ChatMessage
from app.ai.factory import build_provider
from app.ai.token_estimator import estimate_df_tokens, estimate_text_tokens
from app.config.models import AppConfig, DatabaseConfig
from app.ui.theme import ACCENT, BORDER, SURFACE, TEXT, TEXT_MUTED
from app.ui.views._header import ViewHeader
from app.ui.widgets.cards import KpiCard
from app.ui.widgets.sales_filter_bar import SalesFilterBar


SYSTEM_PROMPT = (
    "You are an analytical assistant for a sales manager at a flooring "
    "distributor. You will be given a CSV of invoiced sales lines (filtered "
    "to one or more cost centers and a date range). Answer the user's question "
    "concisely, citing exact numbers from the data. If the data does not "
    "support the answer, say so explicitly. Format dollar amounts with $ and "
    "thousands separators. When listing reps, use the salesperson_desc field."
)

# Soft caps to avoid blowing token budgets on huge result sets.
MAX_ROWS_FOR_AI = 1500


class _AskWorker(QThread):
    answered = Signal(str, dict)
    failed = Signal(str)

    def __init__(self, cfg: AppConfig, system: str, user: str) -> None:
        super().__init__()
        self._cfg, self._system, self._user = cfg, system, user

    def run(self) -> None:
        try:
            provider = build_provider(self._cfg.ai)
            res = provider.complete(
                [ChatMessage("system", self._system),
                 ChatMessage("user", self._user)],
                model=self._cfg.ai.model,
                max_output_tokens=self._cfg.ai.max_output_tokens,
                temperature=self._cfg.ai.temperature,
                timeout_seconds=self._cfg.ai.request_timeout_seconds,
            )
            self.answered.emit(res.text or "(empty response)", res.usage or {})
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class AIChatView(QWidget):
    def __init__(self, cfg: AppConfig, get_db: Callable[[], DatabaseConfig], parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._df: pd.DataFrame | None = None
        self._worker: _AskWorker | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)

        root.addWidget(
            ViewHeader(
                "Ask the AI",
                "Ask any question about the currently-loaded invoiced sales. "
                "Pick cost centers + dates, press Run, then type your question.",
            )
        )

        # KPI estimates
        kpi_row = QHBoxLayout()
        self.kpi_rows = KpiCard("Rows in scope", "—")
        self.kpi_data_tokens = KpiCard("Est. data tokens", "—")
        self.kpi_total_tokens = KpiCard("Est. prompt tokens", "—",
                                        "system + question + data")
        for k in (self.kpi_rows, self.kpi_data_tokens, self.kpi_total_tokens):
            kpi_row.addWidget(k, 1)
        root.addLayout(kpi_row)

        body = QHBoxLayout()
        body.setSpacing(12)
        self.filter_bar = SalesFilterBar(get_db)
        self.filter_bar.sales_loaded.connect(self._on_loaded)
        body.addWidget(self.filter_bar)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(8)

        self.transcript = QTextBrowser()
        self.transcript.setOpenExternalLinks(False)
        self.transcript.setStyleSheet(
            f"QTextBrowser {{ background: {SURFACE}; border: 1px solid {BORDER};"
            f" border-radius: 8px; padding: 14px; color: {TEXT}; }}"
        )
        self.transcript.setHtml(
            f"<p style='color:{TEXT_MUTED}'>No conversation yet. Load sales "
            "data on the left and ask a question below.</p>"
        )
        rv.addWidget(self.transcript, 1)

        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("e.g. Which 5 reps grew the most vs. last 30 days?")
        self.input.setFixedHeight(96)
        self.input.textChanged.connect(self._refresh_token_estimate)
        rv.addWidget(self.input)

        actions = QHBoxLayout()
        self.ask_btn = QPushButton("Ask")
        self.ask_btn.setProperty("primary", True)
        self.ask_btn.setEnabled(False)
        self.ask_btn.clicked.connect(self._ask)
        actions.addWidget(self.ask_btn)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(lambda: self.transcript.setHtml(""))
        actions.addWidget(self.clear_btn)
        actions.addStretch(1)
        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {TEXT_MUTED};")
        actions.addWidget(self.status)
        rv.addLayout(actions)

        body.addWidget(right, 1)
        root.addLayout(body, 1)

    # --------------------------------------------------------------- data
    def _on_loaded(self, df: pd.DataFrame) -> None:
        self._df = df
        self.ask_btn.setEnabled(not df.empty)
        self._refresh_token_estimate()

    def _refresh_token_estimate(self) -> None:
        rows = 0 if self._df is None else len(self._df)
        self.kpi_rows.set_value(f"{rows:,}")
        data_tok = estimate_df_tokens(self._df, max_rows=MAX_ROWS_FOR_AI) if self._df is not None else 0
        sys_tok = estimate_text_tokens(SYSTEM_PROMPT)
        q_tok = estimate_text_tokens(self.input.toPlainText())
        total = sys_tok + q_tok + data_tok
        self.kpi_data_tokens.set_value(
            f"{data_tok:,}",
            "" if rows <= MAX_ROWS_FOR_AI else f"top {MAX_ROWS_FOR_AI:,} of {rows:,} rows",
        )
        self.kpi_total_tokens.set_value(f"{total:,}")

    # --------------------------------------------------------------- ask
    def _ask(self) -> None:
        if self._df is None or self._df.empty:
            return
        question = self.input.toPlainText().strip()
        if not question:
            self.status.setText("Type a question first.")
            return

        sample = self._df.head(MAX_ROWS_FOR_AI)
        buf = io.StringIO()
        sample.to_csv(buf, index=False)
        csv_text = buf.getvalue()

        s, e = self.filter_bar.date_range()
        ccs = self.filter_bar.selected_codes() or ["ALL"]
        user_msg = (
            f"Date range (invoice date): {s.isoformat()} to {e.isoformat()}\n"
            f"Cost centers in scope: {', '.join(ccs)}\n"
            f"Total rows in full dataset: {len(self._df):,} "
            f"(showing {len(sample):,} in CSV below).\n\n"
            f"Question: {question}\n\n"
            f"DATA (CSV):\n{csv_text}"
        )

        self.transcript.append(
            f"<p style='margin:8px 0;'><b style='color:{ACCENT}'>You:</b> "
            f"{question}</p>"
        )
        self.input.clear()
        self.status.setText("Asking the model…")
        self.ask_btn.setEnabled(False)

        self._worker = _AskWorker(self._cfg, SYSTEM_PROMPT, user_msg)
        self._worker.answered.connect(self._on_answer)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_answer(self, text: str, usage: dict) -> None:
        self.ask_btn.setEnabled(True)
        usage_bits = []
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if k in usage:
                usage_bits.append(f"{k.replace('_', ' ')}: {usage[k]:,}")
        usage_str = " · ".join(usage_bits) or "no usage data"
        self.status.setText(usage_str)
        # Render as paragraphs, preserving line breaks.
        html = "<br>".join(
            line.replace("<", "&lt;").replace(">", "&gt;")
            for line in text.splitlines()
        )
        self.transcript.append(f"<p style='margin:8px 0;'><b>Assistant:</b> {html}</p>")

    def _on_failed(self, msg: str) -> None:
        self.ask_btn.setEnabled(True)
        self.status.setText(f"Failed — {msg}")
