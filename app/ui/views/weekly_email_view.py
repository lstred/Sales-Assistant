"""Weekly Sales Email composer.

Generates one **personalised**, **AI-coached** draft per rep that reads like
something a thoughtful sales manager would write \u2014 not a templated KPI dump.

Pipeline:
1. Filter bar loads blended sales for the chosen scope (current + prior year).
2. The view kicks off background loaders for *related* data \u2014 rep
   assignments, display placements, and sample-CC sales \u2014 sharing the
   same scope.
3. :mod:`app.services.manager_analytics` rolls everything into per-rep
   scorecards (revenue, YoY, peer comparison, last-3-months momentum,
   stale/new accounts, core-display coverage, samples-per-account).
4. For each rep, an AI prompt is built with their scorecard + their own
   sales rows. The configured AI provider drafts the email; we fall back
   to a deterministic template if AI is unavailable so the workflow never
   blocks.
5. The right pane lists drafts; the manager reviews and (later) sends
   through :mod:`app.notifications.email_client`.

Extras:
* If the period contains the start of a new fiscal month / quarter / year,
  the per-rep emails get a "Period overview" preamble and the *Master
  leaderboard* email gets a richer top section.
* "Master leaderboard" produces one email summarising every rep's *last
  full week* in descending order, each with a positive AI-written shout-out.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Callable

import pandas as pd
from PySide6.QtCore import QMimeData, QThread, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.ai.base import ChatMessage
from app.ai.factory import build_provider
from app.config.models import AppConfig, DatabaseConfig
from app.data.loaders import (
    load_blended_sales,
    load_display_placements,
    load_price_class_lookup,
    load_rep_assignments,
)
from app.notifications.email_client import EmailClient
from app.services.fiscal_calendar import build_fiscal_year, find_period
from app.services.manager_analytics import (
    PeriodOverview,
    RepScorecard,
    compute_period_overview,
    compute_rep_scorecards,
    current_week_range,
    format_account_label,
    previous_week_range,
    revenue_in_window,
)
from app.services.singleflight import sales_singleflight
from app.ui.theme import BORDER, SURFACE, TEXT, TEXT_MUTED
from app.ui.views._header import ViewHeader
from app.ui.widgets.sales_filter_bar import SalesFilterBar


log = logging.getLogger(__name__)

MASTER_KEY = "__MASTER__"

# Rep names (lower-cased) that should never appear in the leaderboard or shoutouts.
_EXCLUDED_REPS: frozenset[str] = frozenset({"", "house account", "(legacy / pre-aug 2025)"})


# ============================================================ background work
class _ContextLoader(QThread):
    """Loads the side-data (assignments, displays, samples) the analytics
    need on top of the main filter-bar sales DataFrame."""

    loaded = Signal(object, object, object)  # assignments, displays, samples
    failed = Signal(str)

    def __init__(
        self,
        db: DatabaseConfig,
        start: date,
        end: date,
        cost_centers: list[str],
        six_week_january_years: list[int],
    ) -> None:
        super().__init__()
        self._db = db
        self._start, self._end = start, end
        self._ccs = cost_centers
        self._sw = six_week_january_years

    def run(self) -> None:
        try:
            assignments = load_rep_assignments(self._db)
        except Exception as exc:  # noqa: BLE001
            log.warning("rep_assignments load failed: %s", exc)
            assignments = pd.DataFrame()
        try:
            displays = load_display_placements(self._db)
        except Exception as exc:  # noqa: BLE001
            log.warning("display_placements load failed: %s", exc)
            displays = pd.DataFrame()
        try:
            ccs_key = ""  # samples are loaded scope-wide; product-CC filter
                          # would zero them out (a product CC selection like
                          # '010,011' is mutually exclusive with prefix '1').
            key = (
                "blended", self._start.isoformat(), self._end.isoformat(),
                ccs_key, "1",
            )
            samples = sales_singleflight.do(
                key,
                lambda: load_blended_sales(
                    self._db, self._start, self._end, None,
                    self._sw, "1",
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("sample sales load failed: %s", exc)
            samples = pd.DataFrame()
        self.loaded.emit(assignments, displays, samples)


class _AIDraftWorker(QThread):
    """Background worker that asks the AI to draft one email body per rep."""

    drafted = Signal(str, str)  # rep_key, html
    finished_all = Signal()
    failed = Signal(str, str)   # rep_key, error msg

    def __init__(self, cfg: AppConfig, jobs: list[tuple[str, str, str]]) -> None:
        super().__init__()
        self._cfg = cfg
        self._jobs = jobs

    def run(self) -> None:
        try:
            provider = build_provider(self._cfg.ai)
        except Exception as exc:  # noqa: BLE001
            for rep_key, _, _ in self._jobs:
                self.failed.emit(rep_key, f"{type(exc).__name__}: {exc}")
            self.finished_all.emit()
            return
        for rep_key, system, user in self._jobs:
            try:
                res = provider.complete(
                    [ChatMessage("system", system), ChatMessage("user", user)],
                    model=self._cfg.ai.model,
                    max_output_tokens=self._cfg.ai.max_output_tokens,
                    temperature=self._cfg.ai.temperature,
                    timeout_seconds=self._cfg.ai.request_timeout_seconds,
                )
                self.drafted.emit(rep_key, (res.text or "").strip())
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(rep_key, f"{type(exc).__name__}: {exc}")
        self.finished_all.emit()


# ============================================================ send review dialog
class _SendWorker(QThread):
    """Sends a batch of emails sequentially; emits per-item results."""

    result = Signal(str, bool, str)   # key, ok, message
    finished_all = Signal()

    def __init__(self, jobs: list[tuple[str, dict]], client: EmailClient) -> None:
        super().__init__()
        self._jobs = jobs
        self._client = client

    def run(self) -> None:
        for key, draft in self._jobs:
            try:
                res = self._client.send(
                    to_address=draft["to"],
                    subject=draft["subject"],
                    body_text="",           # html-only; email clients fall back gracefully
                    body_html=draft.get("body_html", ""),
                    cc_address=draft.get("cc", ""),
                )
                self.result.emit(key, res.ok, res.error)
            except Exception as exc:  # noqa: BLE001
                self.result.emit(key, False, str(exc))
        self.finished_all.emit()


class _SendReviewDialog(QDialog):
    """Modal dialog: review drafts, select which to send, confirm & dispatch."""

    def __init__(self, drafts: dict, cfg: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self._drafts = drafts
        self._cfg = cfg
        self._worker: _SendWorker | None = None
        self._checkboxes: dict[str, QCheckBox] = {}
        self._status_labels: dict[str, QLabel] = {}

        self.setWindowTitle("Review & Send Emails")
        self.setMinimumSize(1000, 620)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(14)

        # ---- header info ------------------------------------------------
        outbound_ok = cfg.email.enable_outbound_send and bool(cfg.email.smtp_host)
        if outbound_ok:
            info_text = (
                f"<b>Outbound sending is enabled</b> · SMTP: {cfg.email.smtp_host}:{cfg.email.smtp_port}"
                f"{'  ·  Redirecting all to: ' + cfg.email.redirect_all_to if cfg.email.redirect_all_to else ''}"
            )
            info_style = "background:#DCFCE7;border:1px solid #86EFAC;border-radius:6px;padding:8px 12px;font-size:12px;"
        else:
            reason = ("SMTP not configured" if not cfg.email.smtp_host
                      else "outbound sending is disabled in Email settings")
            info_text = f"<b>Sending unavailable</b> — {reason}. Open Email settings to configure."
            info_style = "background:#FEF3C7;border:1px solid #FCD34D;border-radius:6px;padding:8px 12px;font-size:12px;"
        info = QLabel(info_text)
        info.setStyleSheet(info_style)
        info.setWordWrap(True)
        root.addWidget(info)

        # ---- body: checklist left, preview right -------------------------
        body = QHBoxLayout()
        body.setSpacing(12)

        # Left: scrollable checklist
        left = QFrame()
        left.setStyleSheet(f"QFrame {{ border: 1px solid {BORDER}; border-radius: 8px; background: {SURFACE}; }}")
        left.setFixedWidth(340)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(8, 8, 8, 8)
        left_lay.setSpacing(4)

        sel_row = QHBoxLayout()
        sel_all = QPushButton("Select all")
        sel_all.setFixedHeight(26)
        sel_all.clicked.connect(lambda: self._set_all(True))
        desel_all = QPushButton("Deselect all")
        desel_all.setFixedHeight(26)
        desel_all.clicked.connect(lambda: self._set_all(False))
        sel_row.addWidget(sel_all)
        sel_row.addWidget(desel_all)
        sel_row.addStretch()
        left_lay.addLayout(sel_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        inner_lay = QVBoxLayout(inner)
        inner_lay.setContentsMargins(4, 4, 4, 4)
        inner_lay.setSpacing(2)

        rep_keys = [k for k in drafts if k != MASTER_KEY]
        if MASTER_KEY in drafts:
            rep_keys = [MASTER_KEY] + rep_keys

        for key in rep_keys:
            d = drafts[key]
            has_email = bool(d.get("to"))
            row_w = QWidget()
            row_lay = QHBoxLayout(row_w)
            row_lay.setContentsMargins(4, 3, 4, 3)
            row_lay.setSpacing(8)

            cb = QCheckBox()
            cb.setChecked(has_email)
            cb.setEnabled(has_email)
            cb.stateChanged.connect(self._update_send_btn)
            self._checkboxes[key] = cb
            row_lay.addWidget(cb)

            lbl = QLabel(
                f"<b>{d['rep_name']}</b><br>"
                f"<span style='color:#64748B;font-size:11px;'>"
                f"{d['to'] if has_email else '<i>no email on file</i>'}</span>"
            )
            lbl.setWordWrap(False)
            lbl.setTextFormat(Qt.TextFormat.RichText)
            lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            lbl.mousePressEvent = lambda _, k=key: self._preview_key(k)
            row_lay.addWidget(lbl, 1)

            st = QLabel("")
            st.setFixedWidth(60)
            st.setAlignment(Qt.AlignmentFlag.AlignCenter)
            st.setStyleSheet("font-size: 11px; font-weight: 600;")
            self._status_labels[key] = st
            row_lay.addWidget(st)

            inner_lay.addWidget(row_w)

        inner_lay.addStretch()
        scroll.setWidget(inner)
        left_lay.addWidget(scroll, 1)
        body.addWidget(left)

        # Right: preview browser
        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(False)
        self.preview.setStyleSheet(
            f"QTextBrowser {{ background: {SURFACE}; border: 1px solid {BORDER};"
            f" border-radius: 8px; padding: 14px; }}"
        )
        body.addWidget(self.preview, 1)
        root.addLayout(body, 1)

        # ---- status bar -------------------------------------------------
        self._status_bar = QLabel("")
        self._status_bar.setStyleSheet(f"font-size: 11px; color: {TEXT_MUTED};")
        root.addWidget(self._status_bar)

        # ---- buttons ----------------------------------------------------
        btn_row = QHBoxLayout()
        self.send_btn = QPushButton("Send Selected")
        self.send_btn.setProperty("primary", True)
        self.send_btn.setEnabled(outbound_ok)
        self.send_btn.clicked.connect(self._send)
        btn_row.addWidget(self.send_btn)
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

        # Preview first item
        if rep_keys:
            self._preview_key(rep_keys[0])
        self._update_send_btn()

    # ---------------------------------------------------------------- helpers
    def _set_all(self, state: bool) -> None:
        for key, cb in self._checkboxes.items():
            if cb.isEnabled():
                cb.setChecked(state)

    def _update_send_btn(self) -> None:
        n = sum(1 for cb in self._checkboxes.values() if cb.isChecked())
        self.send_btn.setText(f"Send Selected ({n})" if n else "Send Selected")
        self.send_btn.setEnabled(
            n > 0
            and self._cfg.email.enable_outbound_send
            and bool(self._cfg.email.smtp_host)
            and (self._worker is None)
        )

    def _preview_key(self, key: str) -> None:
        d = self._drafts.get(key)
        if not d:
            return
        no_email = d["to"] or "(no email on file)"
        header = (
            f"<div style='color:{TEXT_MUTED};font-size:12px;margin-bottom:8px;'>"
            f"<b>To:</b> {no_email}<br>"
            + (f"<b>Cc:</b> {d['cc']}<br>" if d.get("cc") else "")
            + f"<b>Subject:</b> {d['subject']}</div>"
        )
        self.preview.setHtml(header + d.get("body_html", ""))

    # ---------------------------------------------------------------- send
    def _send(self) -> None:
        jobs = [
            (k, self._drafts[k])
            for k, cb in self._checkboxes.items()
            if cb.isChecked()
        ]
        if not jobs:
            return

        self.send_btn.setEnabled(False)
        self._status_bar.setText(f"Sending {len(jobs)} email(s)…")
        for k in self._checkboxes:
            self._status_labels[k].setText("")

        client = EmailClient(self._cfg.email)
        self._worker = _SendWorker(jobs, client)
        self._worker.result.connect(self._on_result)
        self._worker.finished_all.connect(self._on_finished)
        self._worker.start()

    def _on_result(self, key: str, ok: bool, error: str) -> None:
        lbl = self._status_labels.get(key)
        if lbl is None:
            return
        if ok:
            lbl.setText("✓ Sent")
            lbl.setStyleSheet("font-size:11px;font-weight:600;color:#16A34A;")
        else:
            lbl.setText("✗ Failed")
            lbl.setToolTip(error)
            lbl.setStyleSheet("font-size:11px;font-weight:600;color:#DC2626;")

    def _on_finished(self) -> None:
        sent = sum(
            1 for k, lbl in self._status_labels.items()
            if "Sent" in lbl.text()
        )
        failed = sum(
            1 for k, lbl in self._status_labels.items()
            if "Failed" in lbl.text()
        )
        self._status_bar.setText(
            f"Done — {sent} sent, {failed} failed."
            + (" Hover ✗ Failed items to see the error." if failed else "")
        )
        self._worker = None
        self._update_send_btn()


# ============================================================ view
class WeeklyEmailView(QWidget):
    busy_state_changed = Signal(str)

    def __init__(self, cfg: AppConfig, get_db: Callable[[], DatabaseConfig], parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._get_db = get_db
        self._df: pd.DataFrame | None = None
        self._prior_df: pd.DataFrame | None = None
        self._assignments_df: pd.DataFrame | None = None
        self._displays_df: pd.DataFrame | None = None
        self._samples_df: pd.DataFrame | None = None
        self._price_class_lookup: dict[str, str] = {}
        self._scorecards: dict[str, RepScorecard] = {}
        self._period_overview: PeriodOverview | None = None
        self._drafts: dict[str, dict] = {}
        self._context_loaders: list[_ContextLoader] = []
        self._ai_workers: list[_AIDraftWorker] = []
        self._pending_ai_jobs = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)

        _outbound_note = (
            "Outbound sending is enabled \u2014 use \u2018Queue for review\u2019 to send."
            if cfg.email.enable_outbound_send
            else "Outbound stays disabled until you flip it on in Email settings."
        )
        root.addWidget(
            ViewHeader(
                "Weekly Sales Email",
                f"AI-coached, manager-style drafts \u2014 one per rep \u2014 plus an "
                f"optional master leaderboard email. {_outbound_note}",
            )
        )

        body = QHBoxLayout()
        body.setSpacing(12)
        self.filter_bar = SalesFilterBar(get_db, cfg=cfg, code_prefix_filter="0", page_id="weekly_email")
        self.filter_bar.sales_loaded_with_prior.connect(self._on_sales_loaded)
        self.filter_bar.busy_state_changed.connect(self.busy_state_changed.emit)
        body.addWidget(self.filter_bar)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(8)

        actions = QHBoxLayout()
        self.gen_btn = QPushButton("Generate AI drafts")
        self.gen_btn.setProperty("primary", True)
        self.gen_btn.setEnabled(False)
        self.gen_btn.clicked.connect(self._generate_all)
        actions.addWidget(self.gen_btn)
        self.master_btn = QPushButton("Generate master leaderboard")
        self.master_btn.setEnabled(False)
        self.master_btn.clicked.connect(self._generate_master)
        actions.addWidget(self.master_btn)
        self.copy_btn = QPushButton("📋 Copy leaderboard")
        self.copy_btn.setEnabled(False)
        self.copy_btn.setToolTip("Copy leaderboard as rich HTML — paste directly into Outlook or Gmail")
        self.copy_btn.clicked.connect(self._copy_leaderboard)
        actions.addWidget(self.copy_btn)
        self.email_lb_btn = QPushButton("📧 Email leaderboard")
        self.email_lb_btn.setEnabled(False)
        self.email_lb_btn.setToolTip("Send the leaderboard email via SMTP")
        self.email_lb_btn.clicked.connect(self._email_leaderboard)
        actions.addWidget(self.email_lb_btn)
        self.queue_btn = QPushButton("Queue for review")
        self.queue_btn.setEnabled(False)
        self.queue_btn.clicked.connect(self._queue)
        actions.addWidget(self.queue_btn)
        actions.addStretch(1)
        self.busy_label = QLabel("")
        self.busy_label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        actions.addWidget(self.busy_label)
        rv.addLayout(actions)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.list = QListWidget()
        self.list.itemSelectionChanged.connect(self._show_selected)
        self.list.setMinimumWidth(280)
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
    def _on_sales_loaded(self, df: pd.DataFrame, prior: pd.DataFrame | None) -> None:
        self._df = df
        self._prior_df = prior
        self._scorecards = {}
        self._period_overview = None
        ready = isinstance(df, pd.DataFrame) and not df.empty
        self.gen_btn.setEnabled(ready)
        self.master_btn.setEnabled(ready)
        self.queue_btn.setEnabled(False)
        self.list.clear()
        self.preview.clear()
        if not ready:
            return
        n_prior = (len(prior) if isinstance(prior, pd.DataFrame) else 0) or 0
        self.preview.setPlainText(
            f"Loaded {len(df):,} invoiced lines (current) and "
            f"{n_prior:,} prior-year lines. Loading rep assignments, displays, "
            "and sample sales for full-context drafts\u2026"
        )
        s, e = self.filter_bar.date_range()
        ccs = self.filter_bar.selected_codes()
        sw = list(self._cfg.fiscal.six_week_january_years)
        loader = _ContextLoader(self._get_db(), s, e, ccs, sw)
        loader.loaded.connect(self._on_context_loaded)
        self._context_loaders.append(loader)
        loader.finished.connect(
            lambda L=loader: self._context_loaders.remove(L) if L in self._context_loaders else None
        )
        loader.start()

    def _on_context_loaded(
        self,
        assignments: pd.DataFrame,
        displays: pd.DataFrame,
        samples: pd.DataFrame,
    ) -> None:
        self._assignments_df = assignments
        self._displays_df = displays
        self._samples_df = samples
        self.preview.setPlainText(
            f"Context ready: "
            f"{0 if assignments is None else len(assignments):,} rep\u00d7account assignments, "
            f"{0 if displays is None else len(displays):,} display placements, "
            f"{0 if samples is None else len(samples):,} sample lines.\n\n"
            "Click \u201cGenerate AI drafts\u201d to compose one personalised email "
            "per rep, or \u201cGenerate master leaderboard\u201d for the all-reps recap."
        )

    # --------------------------------------------------------------- analytics
    def _ensure_scorecards(self) -> None:
        if self._scorecards or self._df is None:
            return
        # Lazily load the price class lookup (small, static reference table).
        if not self._price_class_lookup:
            try:
                self._price_class_lookup = load_price_class_lookup(self._get_db())
            except Exception as exc:  # noqa: BLE001
                log.warning("price class lookup failed: %s", exc)
        self._scorecards = compute_rep_scorecards(
            self._df,
            prior_df=self._prior_df,
            assignments_df=self._assignments_df,
            displays_df=self._displays_df,
            samples_df=self._samples_df,
            core_displays_by_cc=self._cfg.core_displays_by_cc,
            sample_to_product_cc=self._cfg.sample_to_product_cc,
            price_class_lookup=self._price_class_lookup or None,
        )
        s, e = self.filter_bar.date_range()
        try:
            period = find_period(e, self._cfg.fiscal.six_week_january_years)
        except Exception:  # noqa: BLE001
            period = None
        if period is not None:
            label = f"FY{period.fiscal_year} P{period.period} ({period.name})"
            self._period_overview = compute_period_overview(
                label, period.start, period.end, self._df, self._prior_df,
            )

    # --------------------------------------------------------------- per-rep drafts
    def _generate_all(self) -> None:
        if self._df is None or self._df.empty:
            return
        self._ensure_scorecards()
        self._drafts.clear()
        self.list.clear()

        s, e = self.filter_bar.date_range()
        ccs = self.filter_bar.selected_codes()
        # Build a human-readable CC label including product-line names so
        # reps see e.g. "CARPET RESIDENTIAL · CARPET COMMERCIAL" not just codes.
        cc_df = getattr(self.filter_bar.cc, "_df", None)
        if cc_df is not None and not cc_df.empty and ccs:
            name_map: dict[str, str] = dict(
                zip(cc_df["cost_center"].astype(str),
                    cc_df["cost_center_name"].fillna(""))
            )
            cc_label = "  ·  ".join(
                name_map.get(c, c) or c for c in ccs
            )
        else:
            cc_label = ", ".join(ccs) if ccs else "all product lines"

        rep_to_number = self._build_rep_number_map()

        ai_jobs: list[tuple[str, str, str]] = []
        for rep_key, sc in sorted(
            self._scorecards.items(), key=lambda kv: -kv[1].revenue
        ):
            if not rep_key:
                continue
            slmn = rep_to_number.get(rep_key, "")
            email = self._cfg.rep_emails.get(slmn, "") if slmn else ""
            cc_email = self._cfg.rep_boss_emails.get(slmn, "") if slmn else ""
            tone = int(self._cfg.rep_tone.get(slmn, 0)) if slmn else 0
            week_lines = self._weekly_lines_for(rep_key)
            subject = (
                f"{rep_key.split()[0] if rep_key else 'Sales'} \u2014 sales recap, "
                f"{s.isoformat()} \u2192 {e.isoformat()}"
            )
            self._drafts[rep_key] = {
                "rep_name": rep_key,
                "salesman_number": slmn,
                "to": email,
                "cc": cc_email,
                "subject": subject,
                "body_html": _render_loading_html(rep_key),
                "scorecard": sc.as_dict(),
                "week_lines": week_lines,
                "cc_label": cc_label,
            }
            label = self._list_label(rep_key, sc, email)
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, rep_key)
            self.list.addItem(item)

            system_msg, user_msg = _build_rep_prompt(
                rep_key=rep_key,
                scorecard=sc,
                period_overview=self._period_overview,
                cc_label=cc_label,
                start=s, end=e,
                week_lines=week_lines,
                tone=tone,
            )
            ai_jobs.append((rep_key, system_msg, user_msg))

        if not ai_jobs:
            self.preview.setPlainText("No reps in scope.")
            return

        if not self._has_ai():
            for rep_key, _sys, _usr in ai_jobs:
                self._apply_draft_text(
                    rep_key,
                    _fallback_body(self._scorecards[rep_key],
                                   self._period_overview,
                                   self._drafts[rep_key]["week_lines"]),
                )
            self.busy_label.setText("AI not configured \u2014 used template fallback.")
            self.queue_btn.setEnabled(True)
            if self.list.count():
                self.list.setCurrentRow(0)
            return

        self.gen_btn.setEnabled(False)
        self.master_btn.setEnabled(False)
        self._pending_ai_jobs = len(ai_jobs)
        self.busy_label.setText(f"Drafting {self._pending_ai_jobs} email(s) with AI\u2026")
        worker = _AIDraftWorker(self._cfg, ai_jobs)
        worker.drafted.connect(self._on_ai_drafted)
        worker.failed.connect(self._on_ai_failed)
        worker.finished_all.connect(self._on_ai_all_done)
        self._ai_workers.append(worker)
        worker.finished.connect(
            lambda W=worker: self._ai_workers.remove(W) if W in self._ai_workers else None
        )
        worker.start()
        if self.list.count():
            self.list.setCurrentRow(0)

    def _on_ai_drafted(self, rep_key: str, text: str) -> None:
        self._apply_draft_text(rep_key, text)
        self._pending_ai_jobs = max(0, self._pending_ai_jobs - 1)
        self.busy_label.setText(
            f"{self._pending_ai_jobs} draft(s) remaining\u2026"
            if self._pending_ai_jobs else "All drafts ready."
        )
        items = self.list.selectedItems()
        if items and items[0].data(Qt.ItemDataRole.UserRole) == rep_key:
            self._show_selected()

    def _on_ai_failed(self, rep_key: str, msg: str) -> None:
        log.warning("AI draft failed for %s: %s", rep_key, msg)
        sc = self._scorecards.get(rep_key)
        if sc is not None:
            self._apply_draft_text(
                rep_key,
                _fallback_body(sc, self._period_overview,
                               self._drafts[rep_key]["week_lines"]),
            )
        self._pending_ai_jobs = max(0, self._pending_ai_jobs - 1)

    def _on_ai_all_done(self) -> None:
        self.gen_btn.setEnabled(True)
        self.master_btn.setEnabled(True)
        self.queue_btn.setEnabled(bool(self._drafts))
        if not self.busy_label.text():
            self.busy_label.setText("All drafts ready.")

    def _apply_draft_text(self, rep_key: str, text: str) -> None:
        d = self._drafts.get(rep_key)
        if d is None:
            return
        sc = self._scorecards.get(rep_key)
        d["body_html"] = _wrap_ai_body(
            text or "(empty AI response)",
            scorecard=sc,
            period_overview=self._period_overview,
            week_lines=d.get("week_lines"),
            cc_label=d.get("cc_label", ""),
        )
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it and it.data(Qt.ItemDataRole.UserRole) == rep_key:
                it.setText(self._list_label(rep_key, sc, d["to"]))
                break

    # --------------------------------------------------------------- master email
    def _generate_master(self) -> None:
        if self._df is None or self._df.empty:
            return
        self._ensure_scorecards()

        # Anchor weekly windows to the latest invoice date in scope.
        anchor = self._anchor_date()

        # Week selection rule: if today is Friday (4) or Saturday (5), use
        # the current (in-progress) week; otherwise use the last full week.
        today = date.today()
        if today.weekday() >= 4:  # Friday or Saturday
            wk_start, wk_end_full = current_week_range(anchor)
            wk_end = min(anchor, wk_end_full)
            using_current_week = True
        else:
            wk_start, wk_end = previous_week_range(anchor)
            using_current_week = False

        # Per-rep weekly revenue for the chosen window.
        per_rep_weekly = revenue_in_window(self._df, wk_start, wk_end, by="rep")

        # Fiscal YTD weekly averages (current and prior year).
        fb_start, fb_end = self.filter_bar.date_range()
        weeks_elapsed = max(1.0, (fb_end - fb_start).days / 7.0)

        per_rep_ytd_rev = revenue_in_window(self._df, fb_start, fb_end, by="rep")
        per_rep_ytd_avg = {rep: rev / weeks_elapsed for rep, rev in per_rep_ytd_rev.items()}

        per_rep_prior_ytd_avg: dict[str, float] = {}
        if self._prior_df is not None and not self._prior_df.empty:
            try:
                prior_start = fb_start.replace(year=fb_start.year - 1)
                prior_end = fb_end.replace(year=fb_end.year - 1)
            except ValueError:
                prior_start = fb_start - timedelta(days=365)
                prior_end = fb_end - timedelta(days=365)
            per_rep_prior_ytd_rev = revenue_in_window(
                self._prior_df, prior_start, prior_end, by="rep"
            )
            per_rep_prior_ytd_avg = {
                rep: rev / weeks_elapsed for rep, rev in per_rep_prior_ytd_rev.items()
            }

        # All reps that appear in any column — exclude where both YTD avgs ≤ 0
        # and strip reps with blank names or excluded entries (e.g. HOUSE ACCOUNT).
        all_reps = set(per_rep_weekly) | set(per_rep_ytd_avg) | set(per_rep_prior_ytd_avg)
        active_reps = {
            rep for rep in all_reps
            if rep.strip().lower() not in _EXCLUDED_REPS
            and (per_rep_ytd_avg.get(rep, 0.0) > 0 or per_rep_prior_ytd_avg.get(rep, 0.0) > 0)
        }

        # Leaderboard sorted by weekly revenue descending.
        leaderboard: list[tuple[str, float]] = sorted(
            [(rep, per_rep_weekly.get(rep, 0.0)) for rep in active_reps],
            key=lambda kv: -kv[1],
        )

        # Top 3 for weekly shoutouts (must have non-zero weekly revenue).
        top3_weekly = [(rep, rev) for rep, rev in leaderboard if rev > 0][:3]

        # Dynamic "Most Improved" section — metric and label change based on where
        # we are in the fiscal calendar:
        #   • last week of a period     → completed fiscal month vs same month LY
        #   • last week of a quarter    → completed fiscal quarter vs same quarter LY
        #   • first week of a period    → fiscal YTD avg vs prior YTD avg
        #   • all other weeks           → fiscal MTD avg vs prior MTD avg
        sw = list(self._cfg.fiscal.six_week_january_years)
        imp_mode, imp_label, imp_cur_avg, imp_prior_avg = _compute_improvement_metrics(
            df=self._df,
            prior_df=self._prior_df,
            today=today,
            sw=sw,
            active_reps=active_reps,
            ytd_cur_avg=per_rep_ytd_avg,
            ytd_prior_avg=per_rep_prior_ytd_avg,
        )

        top3_improvement: list[tuple[str, float]] = sorted(
            [
                (rep, imp_cur_avg.get(rep, 0.0) - imp_prior_avg.get(rep, 0.0))
                for rep in active_reps
                if imp_prior_avg.get(rep, 0.0) > 0
            ],
            key=lambda kv: -kv[1],
        )[:3]

        # Shoutout text.
        if self._has_ai():
            shoutouts_weekly = (
                self._ai_shoutouts(top3_weekly, category="weekly_top")
                if top3_weekly else {}
            )
            shoutouts_imp = (
                self._ai_shoutouts(
                    top3_improvement,
                    category=imp_mode,
                    imp_label=imp_label,
                    cur_avg=imp_cur_avg,
                    prior_avg=imp_prior_avg,
                )
                if top3_improvement else {}
            )
        else:
            shoutouts_weekly = {
                rep: _fallback_shoutout(rep, self._scorecards.get(rep))
                for rep, _ in top3_weekly
            }
            shoutouts_imp = {
                rep: (
                    f"Up ${imp_cur_avg.get(rep, 0) - imp_prior_avg.get(rep, 0):,.0f}/wk "
                    f"vs prior year \u2014 building solid momentum."
                )
                for rep, _ in top3_improvement
            }

        anchor_note_needed = anchor < today
        body_html, plain_text = _render_master_html(
            wk_start=wk_start,
            wk_end=wk_end,
            using_current_week=using_current_week,
            per_rep_weekly=per_rep_weekly,
            per_rep_ytd_avg=per_rep_ytd_avg,
            per_rep_prior_ytd_avg=per_rep_prior_ytd_avg,
            leaderboard=leaderboard,
            top3_weekly=top3_weekly,
            top3_improvement=top3_improvement,
            imp_label=imp_label,
            imp_cur_avg=imp_cur_avg,
            imp_prior_avg=imp_prior_avg,
            shoutouts_weekly=shoutouts_weekly,
            shoutouts_imp=shoutouts_imp,
            period_overview=self._period_overview,
            anchor=anchor if anchor_note_needed else None,
            fb_start=fb_start,
            fb_end=fb_end,
        )

        week_label = f"{wk_start.isoformat()} → {wk_end.isoformat()}"
        subject = f"Team scoreboard — week of {wk_start.isoformat()}"
        self._drafts[MASTER_KEY] = {
            "rep_name": "All reps (master leaderboard)",
            "salesman_number": "",
            "to": "",
            "cc": "",
            "subject": subject,
            "body_html": body_html,
            "plain_text": plain_text,
            "scorecard": {},
            "week_lines": {},
        }

        existing = self.list.findItems("\u2605 Master leaderboard", Qt.MatchFlag.MatchStartsWith)
        for it in existing:
            self.list.takeItem(self.list.row(it))

        item = QListWidgetItem(f"\u2605 Master leaderboard  ({week_label})")
        item.setData(Qt.ItemDataRole.UserRole, MASTER_KEY)
        self.list.insertItem(0, item)
        self.list.setCurrentRow(0)
        self.queue_btn.setEnabled(True)

    def _ai_shoutouts(
        self,
        entries: list[tuple[str, float]],
        *,
        category: str = "weekly_top",
        imp_label: str = "",
        cur_avg: dict[str, float] | None = None,
        prior_avg: dict[str, float] | None = None,
        # Legacy kwargs kept for any remaining call sites
        ytd_avg: dict[str, float] | None = None,
        prior_ytd_avg: dict[str, float] | None = None,
    ) -> dict[str, str]:
        """Generate one-line AI shout-outs for a list of (rep, value) tuples.

        ``category`` controls the framing:
        - ``"weekly_top"``  — celebrate top weekly sellers.
        - ``"month_end"``   — completed fiscal month vs same month last year.
        - ``"quarter_end"`` — completed fiscal quarter vs same quarter last year.
        - ``"ytd_avg"``     — fiscal YTD weekly avg vs prior YTD weekly avg.
        - ``"mtd_avg"``     — fiscal MTD weekly avg vs prior MTD weekly avg.
        """
        # Consolidate legacy parameter names
        _cur = cur_avg or ytd_avg or {}
        _prior = prior_avg or prior_ytd_avg or {}

        provider = build_provider(self._cfg.ai)

        if category == "weekly_top":
            sys_msg = (
                "You are a sales manager writing a one-line, upbeat public shout-out "
                "for each top weekly seller on the team leaderboard. Be specific — "
                "mention dollar amounts and account names where available. "
                "Do NOT use any percentages or percentage signs. "
                "ONE sentence per rep. Format: REP_NAME: shout-out"
            )
            bullets = []
            for rep, rev in entries:
                sc = self._scorecards.get(rep)
                top = (
                    format_account_label(sc.top_growing_accounts[0])
                    if sc and sc.top_growing_accounts else ""
                )
                new_acct = (
                    format_account_label(sc.new_accounts[0])
                    if sc and sc.new_accounts else ""
                )
                context = "  ".join(filter(None, [
                    f"top growing account: {top}" if top else "",
                    f"new account win: {new_acct}" if new_acct else "",
                ]))
                bullets.append(f"- {rep}: ${rev:,.0f} this week  {context}")
        else:
            mode_desc = {
                "month_end":   "the just-completed fiscal month vs the same month last year",
                "quarter_end": "the just-completed fiscal quarter vs the same quarter last year",
                "ytd_avg":     "fiscal year-to-date weekly average vs prior year same period",
                "mtd_avg":     "fiscal month-to-date weekly average vs prior year same period",
            }.get(category, imp_label or "vs prior year")
            sys_msg = (
                f"You are a sales manager publicly recognizing team members who are "
                f"leading in improvement for {mode_desc}. "
                f"Write ONE upbeat, specific sentence per rep that highlights their "
                f"momentum — mention account names and dollar growth where available. "
                f"Do NOT use any percentages or percentage signs. "
                f"Format: REP_NAME: shout-out  (one line per rep)"
            )
            bullets = []
            for rep, delta in entries:
                c = _cur.get(rep, 0.0)
                p = _prior.get(rep, 0.0)
                sc = self._scorecards.get(rep)
                top = (
                    format_account_label(sc.top_growing_accounts[0])
                    if sc and sc.top_growing_accounts else ""
                )
                bullets.append(
                    f"- {rep}: ${c:,.0f}/wk now vs ${p:,.0f}/wk last year "
                    f"(+${delta:,.0f}/wk improvement)"
                    + (f" — driven in large part by {top}" if top else "")
                )

        user_msg = "Team members:\n" + "\n".join(bullets)
        try:
            res = provider.complete(
                [ChatMessage("system", sys_msg), ChatMessage("user", user_msg)],
                model=self._cfg.ai.model,
                max_output_tokens=512,
                temperature=0.65,
                timeout_seconds=self._cfg.ai.request_timeout_seconds,
            )
            out: dict[str, str] = {}
            for line in (res.text or "").splitlines():
                if ":" in line:
                    name, _, msg = line.partition(":")
                    out[name.strip()] = msg.strip()
            return out
        except Exception as exc:  # noqa: BLE001
            log.warning("AI shout-outs failed (%s): %s", category, exc)
            return {}

    # --------------------------------------------------------------- helpers
    def _has_ai(self) -> bool:
        return bool(self._cfg.ai.api_username)

    def _build_rep_number_map(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self._assignments_df is None or self._assignments_df.empty:
            return out
        a = self._assignments_df
        if "salesman_name" not in a.columns or "salesman_number" not in a.columns:
            return out
        for rec in a[["salesman_name", "salesman_number"]].drop_duplicates().to_dict("records"):
            name = str(rec.get("salesman_name") or "").strip()
            num = str(rec.get("salesman_number") or "").strip()
            if name and num:
                out.setdefault(name, num)
        return out

    def _weekly_lines_for(self, rep_key: str) -> dict:
        if self._df is None or self._df.empty:
            return {}
        df = self._df
        df = df[df["salesperson_desc"].astype(str).str.strip() == rep_key]
        if df.empty:
            return {}
        anchor = self._anchor_date()
        prev_s, prev_e = previous_week_range(anchor)
        cur_s, cur_e_full = current_week_range(anchor)
        cur_e = min(anchor, cur_e_full)
        df = df.copy()
        df["invoice_date"] = pd.to_datetime(df["invoice_date"], errors="coerce")
        prev_mask = (df["invoice_date"] >= pd.Timestamp(prev_s)) & (df["invoice_date"] <= pd.Timestamp(prev_e))
        cur_mask = (df["invoice_date"] >= pd.Timestamp(cur_s)) & (df["invoice_date"] <= pd.Timestamp(cur_e))
        return {
            "previous_week_start": prev_s.isoformat(),
            "previous_week_end": prev_e.isoformat(),
            "previous_week_revenue": float(df.loc[prev_mask, "revenue"].sum() or 0),
            "previous_week_lines": int(prev_mask.sum()),
            "current_week_start": cur_s.isoformat(),
            "current_week_end": cur_e.isoformat(),
            "current_week_revenue": float(df.loc[cur_mask, "revenue"].sum() or 0),
            "current_week_lines": int(cur_mask.sum()),
        }

    def _anchor_date(self) -> date:
        """Most-recent invoice date in the loaded data, capped at today.
        Used to anchor weekly windows so the master leaderboard reflects
        the *latest* week that actually has invoices."""
        today = date.today()
        if self._df is None or self._df.empty or "invoice_date" not in self._df.columns:
            return today
        s = pd.to_datetime(self._df["invoice_date"], errors="coerce").dropna()
        if s.empty:
            return today
        latest = s.max().date()
        return min(today, latest)

    def _list_label(self, rep_key: str, sc: RepScorecard | None, email: str) -> str:
        rev = sc.revenue if sc else 0.0
        yoy = "" if sc is None or sc.yoy_pct is None else f"  ({sc.yoy_pct:+.0f}%)"
        suffix = "" if email else "   \u2014 no email on file (set in Sales Reps)"
        return f"{rep_key}  \u00b7  ${rev:,.0f}{yoy}{suffix}"

    # --------------------------------------------------------------- preview / queue
    def _show_selected(self) -> None:
        items = self.list.selectedItems()
        if not items:
            return
        key = items[0].data(Qt.ItemDataRole.UserRole)
        d = self._drafts.get(key)
        if not d:
            return
        is_master = (key == MASTER_KEY)
        self.copy_btn.setEnabled(is_master and bool(d.get("body_html")))
        self.email_lb_btn.setEnabled(is_master and bool(d.get("body_html")))
        no_email = d['to'] or "(no email on file \u2014 set in Sales Reps)"
        header = (
            f"<div style='color:{TEXT_MUTED};font-size:12px;margin-bottom:8px;'>"
            f"<b>To:</b> {no_email}<br>"
            + (f"<b>Cc:</b> {d['cc']}<br>" if d.get('cc') else "")
            + f"<b>Subject:</b> {d['subject']}</div>"
        )
        self.preview.setHtml(header + d["body_html"])

    def _copy_leaderboard(self) -> None:
        """Copy the leaderboard as rich HTML to the clipboard.

        Outlook and Gmail both accept ``text/html`` clipboard data and will
        render the table just like they would from an email, so column
        alignment is preserved without relying on monospace fonts.
        """
        items = self.list.selectedItems()
        if not items:
            return
        key = items[0].data(Qt.ItemDataRole.UserRole)
        d = self._drafts.get(key)
        if not d or not d.get("body_html"):
            return
        html_body = d["body_html"]
        # Wrap in a complete HTML document so clipboard consumers can render it.
        full_html = (
            "<!DOCTYPE html><html><head>"
            "<meta charset='utf-8'>"
            "<style>body{font-family:Calibri,Arial,sans-serif;font-size:14px;}"
            "table{border-collapse:collapse;}"
            "td,th{padding:6px 12px;}"
            "</style></head><body>"
            + html_body
            + "</body></html>"
        )
        mime = QMimeData()
        mime.setHtml(full_html)
        mime.setText(d.get("plain_text", ""))
        QApplication.clipboard().setMimeData(mime)
        orig = self.copy_btn.text()
        self.copy_btn.setText("✓ Copied!")
        self.copy_btn.setEnabled(False)
        QTimer.singleShot(2000, lambda: (
            self.copy_btn.setText(orig),
            self.copy_btn.setEnabled(True),
        ))

    def _email_leaderboard(self) -> None:
        """Prompt for a recipient address and send the leaderboard via SMTP."""
        items = self.list.selectedItems()
        if not items:
            return
        key = items[0].data(Qt.ItemDataRole.UserRole)
        d = self._drafts.get(key)
        if not d or not d.get("body_html"):
            return

        if not self._cfg.email.enable_outbound_send or not self._cfg.email.smtp_host:
            QMessageBox.warning(
                self, "Email not configured",
                "Outbound sending is disabled or SMTP is not configured.\n"
                "Enable it in Settings → Email.",
            )
            return

        addr, ok = QInputDialog.getText(
            self, "Email leaderboard",
            "Send leaderboard to:",
            QLineEdit.EchoMode.Normal,
            self._cfg.email.smtp_from_address,
        )
        if not ok or not addr.strip():
            return
        addr = addr.strip()

        client = EmailClient(self._cfg.email)
        result = client.send(
            to_address=addr,
            subject=d.get("subject", "Team leaderboard"),
            body_text="",
            body_html=d["body_html"],
        )
        if result.ok:
            QMessageBox.information(self, "Sent", f"Leaderboard sent to {addr}.")
        else:
            QMessageBox.critical(self, "Send failed", result.error)


    def _queue(self) -> None:
        if not self._drafts:
            return
        dlg = _SendReviewDialog(self._drafts, self._cfg, parent=self)
        dlg.exec()


# ============================================================ rendering
def _render_loading_html(rep_name: str) -> str:
    return (
        f"<p>Drafting personalised email for <b>{rep_name}</b>\u2026</p>"
        "<p style='color:#888;'>The model is reading their scorecard, "
        "comparing them to peers, and writing the body. This usually "
        "takes a few seconds per rep.</p>"
    )


def _wrap_ai_body(
    text: str,
    *,
    scorecard: RepScorecard | None,
    period_overview: PeriodOverview | None,
    week_lines: dict | None,
    cc_label: str = "",
) -> str:
    body_html = "".join(
        f"<p>{line.replace('<', '&lt;').replace('>', '&gt;')}</p>"
        for line in text.splitlines()
        if line.strip()
    )
    week_html = ""
    if week_lines:
        prev_rev = week_lines.get("previous_week_revenue", 0.0)
        cur_rev = week_lines.get("current_week_revenue", 0.0)
        week_html = (
            f"<div style='background:#F8FAFC;border:1px solid #E2E8F0;"
            f"border-radius:8px;padding:10px 14px;margin:14px 0;font-size:13px;'>"
            f"<b>Last week ({week_lines['previous_week_start']} \u2192 "
            f"{week_lines['previous_week_end']}):</b> ${prev_rev:,.0f} on "
            f"{week_lines['previous_week_lines']:,} lines\u2003\u00b7\u2003"
            f"<b>This week to date:</b> ${cur_rev:,.0f} on "
            f"{week_lines['current_week_lines']:,} lines"
            f"</div>"
        )
    # Product-line coverage note — tells the rep which product categories
    # this email covers so there's no ambiguity.
    cc_html = ""
    if cc_label:
        cc_html = (
            f"<p style='color:#475569;font-size:11px;background:#F8FAFC;"
            f"border:1px solid #E2E8F0;border-radius:6px;padding:5px 10px;"
            f"display:inline-block;margin:0 0 10px 0;'>"
            f"\U0001f4ca <b>Product lines:</b> {cc_label}</p>"
        )
    overview_html = ""
    if period_overview is not None:
        overview_html = (
            f"<p style='color:#475569;font-size:12px;'>"
            f"<b>{period_overview.label}</b>: company-wide revenue "
            f"${period_overview.revenue:,.0f}"
            + ("" if period_overview.yoy_pct is None
               else f" ({period_overview.yoy_pct:+.1f}% YoY)")
            + f" \u00b7 {period_overview.active_reps} active reps.</p>"
        )
    footer = ""
    if scorecard is not None:
        footer = _scorecard_footer_html(scorecard)
    return cc_html + overview_html + week_html + body_html + footer


def _scorecard_footer_html(sc: RepScorecard) -> str:
    yoy = "n/a" if sc.yoy_pct is None else f"{sc.yoy_pct:+.1f}%"
    peers = "n/a" if sc.vs_peers_pct is None else f"{sc.vs_peers_pct:+.1f} pts vs peer avg"
    last3 = "n/a" if sc.last_3mo_vs_prior_3mo_pct is None else f"{sc.last_3mo_vs_prior_3mo_pct:+.1f}%"
    coverage = f"{sc.core_display_coverage_pct:.0f}%"
    samples = f"{sc.samples_per_account:.2f}/account"
    growing = ", ".join(format_account_label(a) for a in sc.top_growing_accounts[:3]) or "\u2014"
    declining = ", ".join(format_account_label(a) for a in sc.top_declining_accounts[:3]) or "\u2014"
    yoy_extra = "  (outlier \u2014 likely territory transfer)" if sc.is_yoy_outlier else ""
    if yoy_extra:
        yoy = yoy + yoy_extra
    return (
        f"<hr style='border:none;border-top:1px solid #E2E8F0;margin:18px 0;'>"
        f"<div style='font-size:12px;color:#334155;'>"
        f"<p style='margin:6px 0;font-weight:600;color:#0F172A;'>Scorecard</p>"
        f"<ul style='margin:6px 0;padding-left:20px;'>"
        f"<li>Revenue: <b>${sc.revenue:,.0f}</b> \u00b7 GP {sc.gpp_pct:.1f}%</li>"
        f"<li>YoY: <b>{yoy}</b> \u00b7 {peers}</li>"
        f"<li>Last 3 months vs prior 3: <b>{last3}</b></li>"
        f"<li>Active accounts: <b>{sc.active_accounts}/{sc.total_accounts}</b> "
        f"({sc.active_account_pct:.0f}%)</li>"
        f"<li>Core-display coverage: <b>{coverage}</b> "
        f"({sc.accounts_with_core_displays}/{sc.total_accounts})</li>"
        f"<li>Samples placed: <b>{sc.sample_lines}</b> ({samples})</li>"
        f"<li>Top growing: {growing}</li>"
        f"<li>Top declining: {declining}</li>"
        f"</ul>"
        f"</div>"
    )


# ============================================================ improvement mode
_IMP_QUARTER_PERIODS = {3, 6, 9, 12}
_IMP_QUARTER_NAMES = {3: "Q1", 6: "Q2", 9: "Q3", 12: "Q4"}


def _compute_improvement_metrics(
    *,
    df: "pd.DataFrame",
    prior_df: "pd.DataFrame | None",
    today: date,
    sw: list[int],
    active_reps: set[str],
    ytd_cur_avg: dict[str, float],
    ytd_prior_avg: dict[str, float],
) -> tuple[str, str, dict[str, float], dict[str, float]]:
    """Determine the "Most Improved" comparison mode and compute per-rep averages.

    Returns ``(mode, label, cur_avg_by_rep, prior_avg_by_rep)`` where all
    monetary values are **weekly averages** ($/week).

    Modes:
    - ``"ytd_avg"``     — 1st week of a new period: fiscal YTD avg vs prior YTD avg.
    - ``"mtd_avg"``     — mid-month: fiscal MTD avg vs prior year same MTD avg.
    - ``"month_end"``   — last week of a period: completed month avg vs prior year.
    - ``"quarter_end"`` — last week of a quarter-close period (3,6,9,12): quarter avg.
    """
    try:
        current_p = find_period(today, sw)
    except Exception:  # noqa: BLE001
        # Fallback: just return YTD
        return ("ytd_avg", "Fiscal YTD Avg/Wk", ytd_cur_avg, ytd_prior_avg)

    days_into = (today - current_p.start).days
    days_left = (current_p.end - today).days
    first_week = days_into < 7
    last_week = days_left < 7
    is_qtr = current_p.period in _IMP_QUARTER_PERIODS

    # ── 1st week of new period: use YTD ──────────────────────────────────────
    if first_week:
        return ("ytd_avg", "Fiscal YTD Avg/Wk", ytd_cur_avg, ytd_prior_avg)

    # ── Last week of period ───────────────────────────────────────────────────
    if last_week and is_qtr:
        mode = "quarter_end"
        q_name = _IMP_QUARTER_NAMES[current_p.period]
        q_start_period = current_p.period - 2  # periods 1,4,7,10
        try:
            fy_periods = build_fiscal_year(current_p.fiscal_year, sw)
            cur_start = fy_periods[q_start_period - 1].start
        except Exception:  # noqa: BLE001
            cur_start = current_p.start
        cur_end = min(today, current_p.end)
        label = f"{q_name} Avg/Wk vs Prior Year"
    elif last_week:
        mode = "month_end"
        cur_start = current_p.start
        cur_end = min(today, current_p.end)
        label = f"{current_p.name} Avg/Wk vs Prior Year"
    else:
        # ── Mid-month: MTD avg ────────────────────────────────────────────────
        mode = "mtd_avg"
        cur_start = current_p.start
        cur_end = today
        label = f"{current_p.name} MTD Avg/Wk vs Prior Year"

    # Prior-year equivalent window
    try:
        prior_start = cur_start.replace(year=cur_start.year - 1)
        prior_end = cur_end.replace(year=cur_end.year - 1)
    except ValueError:
        prior_start = cur_start - timedelta(days=365)
        prior_end = cur_end - timedelta(days=365)

    weeks_cur = max(1.0, (cur_end - cur_start).days / 7.0)
    weeks_prior = max(1.0, (prior_end - prior_start).days / 7.0)

    cur_rev = revenue_in_window(df, cur_start, cur_end, by="rep")
    cur_avg = {
        rep: rev / weeks_cur
        for rep, rev in cur_rev.items()
        if rep in active_reps
    }

    prior_avg: dict[str, float] = {}
    if prior_df is not None and not prior_df.empty:
        pr_rev = revenue_in_window(prior_df, prior_start, prior_end, by="rep")
        prior_avg = {
            rep: rev / weeks_prior
            for rep, rev in pr_rev.items()
            if rep in active_reps
        }

    return (mode, label, cur_avg, prior_avg)


def _render_master_html(
    *,
    wk_start: date,
    wk_end: date,
    using_current_week: bool,
    per_rep_weekly: dict[str, float],
    per_rep_ytd_avg: dict[str, float],
    per_rep_prior_ytd_avg: dict[str, float],
    leaderboard: list[tuple[str, float]],
    top3_weekly: list[tuple[str, float]],
    top3_improvement: list[tuple[str, float]],
    imp_label: str,
    imp_cur_avg: dict[str, float],
    imp_prior_avg: dict[str, float],
    shoutouts_weekly: dict[str, str],
    shoutouts_imp: dict[str, str],
    period_overview: PeriodOverview | None,
    anchor: date | None = None,
    fb_start: date | None = None,
    fb_end: date | None = None,
) -> tuple[str, str]:
    """Render master leaderboard. Returns ``(html, plain_text)``."""

    week_kind = "This week (in progress)" if using_current_week else "Last week"
    period_label = (
        f"{fb_start.isoformat()} → {fb_end.isoformat()}"
        if fb_start and fb_end else "Fiscal YTD"
    )
    medals = ["🥇", "🥈", "🥉"]

    # ---- shoutout sections ------------------------------------------------
    def _shoutout_html(title: str, entries: list[tuple[str, float]],
                       texts: dict[str, str], value_fmt: str) -> str:
        if not entries:
            return ""
        items_html = "".join(
            f"<div style='margin:6px 0;'>"
            f"<span style='font-size:18px;margin-right:8px;'>{medals[i]}</span>"
            f"<strong>{rep}</strong> "
            f"<span style='color:#475569;'>{value_fmt.format(val)}</span>"
            f"</div>"
            for i, (rep, val) in enumerate(entries)
        )
        return (
            f"<div style='background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;"
            f"padding:12px 16px;margin:10px 0;'>"
            f"<div style='font-size:13px;font-weight:700;color:#1D4ED8;margin-bottom:8px;'>"
            f"{title}</div>"
            + items_html
            + "</div>"
        )

    shoutout_weekly_html = _shoutout_html(
        "⭐ Top 3 This Week",
        top3_weekly,
        shoutouts_weekly,
        "— ${:,.0f} in sales",
    )
    shoutout_imp_html = _shoutout_html(
        f"📈 Most Improved — {imp_label}",
        top3_improvement,
        shoutouts_imp,
        "— +${:,.0f}/wk gain",
    )

    # ---- table rows -------------------------------------------------------
    total_weekly = total_ytd_avg = total_prior_ytd_avg = 0.0
    rows_html: list[str] = []
    for i, (rep, _) in enumerate(leaderboard, 1):
        weekly = per_rep_weekly.get(rep, 0.0)
        ytd = per_rep_ytd_avg.get(rep, 0.0)
        prior = per_rep_prior_ytd_avg.get(rep, 0.0)
        total_weekly += weekly
        total_ytd_avg += ytd
        total_prior_ytd_avg += prior
        zebra = "#FFFFFF" if i % 2 else "#F8FAFC"
        rows_html.append(
            f"<tr style='background:{zebra};'>"
            f"<td style='padding:6px 10px;color:#64748B;'>{i}</td>"
            f"<td style='padding:6px 10px;font-weight:600;color:#0F172A;'>{rep}</td>"
            f"<td style='padding:6px 10px;text-align:right;font-variant-numeric:tabular-nums;'>"
            f"{'$' + f'{weekly:,.0f}' if weekly > 0 else '—'}</td>"
            f"<td style='padding:6px 10px;text-align:right;color:#0F172A;font-variant-numeric:tabular-nums;'>"
            f"${ytd:,.0f}</td>"
            f"<td style='padding:6px 10px;text-align:right;color:#475569;font-variant-numeric:tabular-nums;'>"
            f"{'$' + f'{prior:,.0f}' if prior > 0 else '—'}</td>"
            f"</tr>"
        )

    totals_row = (
        "<tr style='background:#F1F5F9;font-weight:700;border-top:2px solid #CBD5E1;'>"
        "<td style='padding:8px 10px;'></td>"
        "<td style='padding:8px 10px;color:#0F172A;'>TOTAL</td>"
        f"<td style='padding:8px 10px;text-align:right;font-variant-numeric:tabular-nums;'>${total_weekly:,.0f}</td>"
        f"<td style='padding:8px 10px;text-align:right;font-variant-numeric:tabular-nums;'>${total_ytd_avg:,.0f}</td>"
        f"<td style='padding:8px 10px;text-align:right;font-variant-numeric:tabular-nums;color:#475569;'>"
        f"{'$' + f'{total_prior_ytd_avg:,.0f}' if total_prior_ytd_avg > 0 else '—'}</td>"
        "</tr>"
    )

    if not rows_html:
        table_body = "<tr><td colspan='5' style='padding:12px;color:#888;'>No sales data in scope.</td></tr>"
    else:
        table_body = "".join(rows_html) + totals_row

    # ---- overview & notes -------------------------------------------------
    overview = ""
    if period_overview is not None and period_overview.revenue:
        overview = (
            f"<p style='font-size:13px;'><b>{period_overview.label} — company recap:</b> "
            f"${period_overview.revenue:,.0f}"
            + ("" if period_overview.yoy_pct is None
               else f" ({period_overview.yoy_pct:+.1f}% YoY)")
            + f" · GP {period_overview.gpp_pct:.1f}% · "
            + f"{period_overview.active_reps} active reps · "
            + f"{period_overview.active_accounts:,} active accounts.</p>"
        )

    anchor_note = ""
    if anchor and anchor < date.today():
        anchor_note = (
            f"<p style='color:#92400E;font-size:11px;background:#FEF3C7;"
            f"border:1px solid #FCD34D;border-radius:6px;padding:6px 10px;"
            f"display:inline-block;margin:6px 0;'>"
            f"⚠ Anchored to last invoice date in scope ({anchor.isoformat()}) "
            f"— widen the date range to include this calendar week."
            f"</p>"
        )

    # ---- assemble HTML ----------------------------------------------------
    html = (
        "<p style='color:#475569;'>Team — here's this week's scoreboard. "
        "Great effort from everyone on the list.</p>"
        + overview
        + anchor_note
        + shoutout_weekly_html
        + shoutout_imp_html
        + f"<h3 style='margin:18px 0 8px 0;font-size:14px;'>"
          f"Leaderboard — {week_kind}: {wk_start.isoformat()} → {wk_end.isoformat()}</h3>"
        + "<table cellpadding='0' cellspacing='0' "
          "style='border-collapse:collapse;width:100%;font-size:13px;"
          "border:1px solid #E2E8F0;border-radius:6px;overflow:hidden;'>"
        + "<thead><tr style='background:#0F172A;color:#F8FAFC;'>"
          "<th style='padding:8px 10px;text-align:left;'>#</th>"
          "<th style='padding:8px 10px;text-align:left;'>Rep</th>"
          f"<th style='padding:8px 10px;text-align:right;'>Weekly Sales</th>"
          f"<th style='padding:8px 10px;text-align:right;'>Fiscal YTD Avg/Wk</th>"
          f"<th style='padding:8px 10px;text-align:right;'>Prev FY YTD Avg/Wk</th>"
          "</tr></thead>"
        + "<tbody>" + table_body + "</tbody></table>"
        + f"<p style='color:#475569;font-size:12px;margin-top:10px;'>"
          f"YTD averages use {period_label}. "
          f"Reps with $0 in both YTD columns are excluded.</p>"
        + "<p style='color:#64748B;font-size:11px;margin-top:4px;'>"
          "Numbers are invoiced lines from the warehouse — open orders are not included.</p>"
    )

    # ---- plain text for clipboard -----------------------------------------
    # Format dates for display (e.g. "May 10, 2026")
    def _fmt_date(d: date) -> str:
        return d.strftime("%b %#d, %Y") if hasattr(d, "strftime") else str(d)

    # Dynamic column widths based on longest rep name
    max_name = max((len(rep) for rep, _ in leaderboard), default=10)
    name_w = max(max_name, 22)
    # Shoutout-section name width (longest across weekly + improvement)
    shout_all = [r for r, _ in top3_weekly] + [r for r, _ in top3_improvement]
    shout_name_w = max((len(r) for r in shout_all), default=18) if shout_all else 18
    shout_name_w = max(shout_name_w, 18)

    rank_w, wk_w, ytd_w, prev_w = 3, 13, 14, 14
    sep = "   "
    col_total = rank_w + name_w + wk_w + ytd_w + prev_w + len(sep) * 4
    heavy = "═" * col_total
    light = "─" * col_total

    def _cell(s: str, w: int, align: str = "right") -> str:
        return s.rjust(w) if align == "right" else s.ljust(w)

    def _row(rank: str, name: str, wk: str, ytd: str, prior: str) -> str:
        return (
            _cell(rank, rank_w) + sep
            + _cell(name, name_w, "left") + sep
            + _cell(wk, wk_w) + sep
            + _cell(ytd, ytd_w) + sep
            + _cell(prior, prev_w)
        )

    # ---- shoutout sections -----------------------------------------------
    medals_txt = ["1.", "2.", "3."]
    plain_lines: list[str] = [
        "WEEKLY TEAM LEADERBOARD",
        f"  Week: {_fmt_date(wk_start)} to {_fmt_date(wk_end)}"
        + (" (in progress)" if using_current_week else ""),
        f"  Period: {period_label}",
        heavy,
    ]

    if top3_weekly:
        # mini-table header
        wk_inner_w = max((len(f"${r:,.0f}") for _, r in top3_weekly), default=10)
        wk_inner_w = max(wk_inner_w, 12)
        plain_lines += [
            "",
            "TOP 3 THIS WEEK",
            f"  {'':>{3}}  {'Rep':<{shout_name_w}}  {'Sales':>{wk_inner_w}}",
            f"  {'─'*3}  {'─'*shout_name_w}  {'─'*wk_inner_w}",
        ]
        for i, (rep, rev) in enumerate(top3_weekly):
            plain_lines.append(
                f"  {medals_txt[i]:>3}  {rep:<{shout_name_w}}  ${rev:>{wk_inner_w-1},.0f}"
            )
        plain_lines.append("")

    if top3_improvement:
        # mini-table for improvement section with Now/Wk | Prev/Wk | +/-/Wk
        ni_w, pi_w, di_w = 12, 12, 12
        plain_lines += [
            f"MOST IMPROVED — {imp_label.upper()}",
            f"  {'':>3}  {'Rep':<{shout_name_w}}  {'Now/Wk':>{ni_w}}  {'Prev/Wk':>{pi_w}}  {'+/-/Wk':>{di_w}}",
            f"  {'─'*3}  {'─'*shout_name_w}  {'─'*ni_w}  {'─'*pi_w}  {'─'*di_w}",
        ]
        for i, (rep, delta) in enumerate(top3_improvement):
            cur = imp_cur_avg.get(rep, 0.0)
            prev = imp_prior_avg.get(rep, 0.0)
            sign = "+" if delta >= 0 else ""
            plain_lines.append(
                f"  {medals_txt[i]:>3}  {rep:<{shout_name_w}}"
                f"  ${cur:>{ni_w-1},.0f}"
                f"  ${prev:>{pi_w-1},.0f}"
                f"  {sign}${abs(delta):>{di_w-2},.0f}"
            )
        plain_lines.append("")

    # ---- main standings table -------------------------------------------
    plain_lines += [
        heavy,
        "FULL STANDINGS",
        heavy,
        _row("#", "Rep", "This Week", "YTD Avg/Wk", "Prev YTD Avg"),
        light,
    ]
    for i, (rep, _) in enumerate(leaderboard, 1):
        weekly = per_rep_weekly.get(rep, 0.0)
        ytd = per_rep_ytd_avg.get(rep, 0.0)
        prior = per_rep_prior_ytd_avg.get(rep, 0.0)
        plain_lines.append(_row(
            str(i),
            rep[:name_w],
            f"${weekly:,.0f}" if weekly > 0 else "—",
            f"${ytd:,.0f}",
            f"${prior:,.0f}" if prior > 0 else "—",
        ))
    plain_lines += [
        light,
        _row("", "TOTAL",
             f"${total_weekly:,.0f}",
             f"${total_ytd_avg:,.0f}",
             f"${total_prior_ytd_avg:,.0f}" if total_prior_ytd_avg > 0 else "—"),
        heavy,
        "",
        "* Invoiced sales only — open orders excluded.",
    ]

    plain_text = "\n".join(plain_lines)
    return html, plain_text


# ============================================================ AI prompts
def _build_rep_prompt(
    *,
    rep_key: str,
    scorecard: RepScorecard,
    period_overview: PeriodOverview | None,
    cc_label: str,
    start: date,
    end: date,
    week_lines: dict,
    tone: int,
) -> tuple[str, str]:
    tone_word = (
        "extra-encouraging and warm" if tone >= 2
        else "supportive but candid" if tone >= 0
        else "direct, results-focused, no fluff" if tone >= -1
        else "firm and clear about underperformance"
    )

    # Classify rep tier from scorecard signals.
    sc = scorecard
    _peer_count = sc.peer_count or 1
    _bottom_40 = (
        sc.rank_revenue is not None and sc.rank_revenue > _peer_count * 0.60
    )
    _declining = sc.yoy_pct is not None and sc.yoy_pct < -5.0
    _low_activation = sc.active_account_pct < 50.0
    is_struggling = _bottom_40 and (_declining or _low_activation)

    if is_struggling:
        section5_instruction = (
            "5. THIS WEEK'S FOCUS: Give ONE specific, high-impact action the rep must "
            "take this week. Format: 'This week: [concrete task] — [account name and "
            "dollar context]'. For underperformers this is an expectation, not a suggestion."
        )
    else:
        section5_instruction = (
            "5. THIS WEEK'S FOCUS: Give ONE simple, high-upside action worth prioritizing "
            "this week. Frame as an opportunity, not an order. Make it specific to their data."
        )

    sys_msg = (
        "You are a senior sales manager at a flooring distributor. "
        "You are generating a WEEKLY TERRITORY PERFORMANCE EMAIL for ONE sales rep. "
        "Your job is NOT to summarize raw data. Your job is to identify meaningful changes, "
        "recognize wins, surface missed opportunities, detect behavioral patterns, and coach "
        "the rep toward actions that will increase sales. "
        "Keep the rep engaged — every email should feel slightly different.\n\n"
        f"TONE: {tone_word}. Rep tier: {'STRUGGLING — be direct about expectations' if is_struggling else 'PERFORMING — be insight-led and motivating'}.\n\n"
        "IMPORTANT RULES:\n"
        "- Total length: 150–250 words. Reps don't read long emails.\n"
        "- Use short sections and bullets. No long paragraphs.\n"
        "- Conversational, direct, motivating, concise. Avoid corporate language.\n"
        "- Do NOT dump raw statistics. Prioritize interesting insights over comprehensive reporting.\n"
        "- Only reference numbers that appear in the data block. Do not invent figures.\n"
        "- Always write full date ranges (e.g. 'February–April 2026'), never 'previous period'.\n"
        "- When citing a sales figure for an account, ALWAYS show BOTH periods: "
        "'$25,239 (Feb–Apr 2025) → $12,548 (Feb–Apr 2026)'. "
        "Never mention just one dollar amount without specifying its time period.\n"
        "- ALWAYS pair account numbers with the account name when mentioning accounts "
        "(e.g. '#1234 · ABC FLOORING' or 'ABC FLOORING (#1234)'). Never cite a number alone.\n"
        "- Use PRODUCT DESCRIPTIONS (e.g. 'Carpet Residential', 'Hardwood') NOT 6-character "
        "price class codes. The data block shows descriptions — use them. If you see an entry "
        "that looks like a raw code (uppercase letters + digits, e.g. 'SEL086'), do NOT use "
        "that code in the email — skip that line or refer to the product category generically.\n"
        "- No subject line. No greeting ('Hi REP'). Start directly with the scoreboard.\n"
        "- Skip the scorecard footer — the system appends it automatically.\n"
        "- HIGH-IMPACT FLAG: any account with >$5,000 decline or a consistent buyer now at "
        "$0 must appear in BIGGEST OPPORTUNITY every week until resolved.\n\n"
        "EMAIL STRUCTURE — write exactly in this order:\n\n"
        "1. QUICK SCOREBOARD (3–5 short lines or bullets):\n"
        "   - Weekly sales + change vs prior week\n"
        "   - MTD or period-to-date vs prior year (use exact months)\n"
        "   - Top product line or category this week\n"
        "   - Any notable ranking movement or display addition\n"
        "   Keep each item to one short line. No editorializing here.\n\n"
        "2. BIGGEST WIN (2–3 sentences):\n"
        "   Highlight one meaningful success. Must cite a real account name + number and "
        "a dollar figure or trend. Examples of good wins: a dormant account that reactivated, "
        "a big growth account, a strong sample-to-sale conversion, a display payoff.\n\n"
        "3. BIGGEST OPPORTUNITY (2–3 sentences):\n"
        "   Identify ONE actionable opportunity in their territory. Could be: a stale account "
        "with strong history, a display account underperforming its potential, a product gap "
        "in a high-volume account, a momentum shift worth chasing. Be specific — name the "
        "account (name + number) and quantify the gap.\n\n"
        "4. COACHING INSIGHT (1–2 sentences):\n"
        "   The most valuable part. One smart observation about a pattern, behavior, or "
        "correlation in their data. Examples: 'Your accounts with core displays are buying "
        "at 2x the rate of those without.' or 'Your top growth accounts this period all share "
        "the same product category — worth expanding that playbook.' Make it feel like "
        "you personally studied their territory.\n\n"
        f"{section5_instruction}\n\n"
        "6. SERVICE OFFER (1 line, optional):\n"
        "   If a specific data question would help the rep act, offer a deeper pull with a "
        "yes/no ask. Example: 'Want a month-by-month breakdown of ABC FLOORING (#1234) since "
        "January 2026? Reply YES.' Only include this if there is a genuinely useful question "
        "— skip it otherwise.\n\n"
        "GOOD EXAMPLES:\n"
        "- 'Your sample placements this quarter are converting into stronger repeat orders.'\n"
        "- 'Accounts with 2+ core displays average significantly higher weekly volume.'\n"
        "- 'Several dormant accounts still carry strong historical sales potential.'\n\n"
        "BAD EXAMPLES (never write these):\n"
        "- 'Please continue your efforts.'\n"
        "- 'Sales were up 3.2%.'\n"
        "- 'Thank you for all you do.'\n"
        "- 'Attached is your weekly summary.'"
    )
    if scorecard.is_yoy_outlier:
        sys_msg += (
            "\n\nIMPORTANT: This rep's YoY % is an outlier (likely a territory "
            "transfer, not real performance). DO NOT frame the email around YoY. "
            "Lead with absolute revenue, GP%, 3-month momentum, top "
            "growing/declining accounts. Mention YoY only as a factual aside."
        )

    overview_block = ""
    if period_overview is not None:
        po = period_overview
        yoy = "n/a" if po.yoy_pct is None else f"{po.yoy_pct:+.1f}%"
        overview_block = (
            f"COMPANY PERIOD OVERVIEW ({po.label}, {po.start.strftime('%B %Y')} -> {po.end.strftime('%B %Y')}):\n"
            f"  total_revenue=${po.revenue:,.0f}, prior=${po.prior_revenue:,.0f}, "
            f"yoy={yoy}, gp%={po.gpp_pct:.1f}, active_reps={po.active_reps}\n\n"
        )

    # Build explicit human-readable date range labels so the AI can write
    # them out in full (e.g. "February–April 2026") — never "previous period".
    start_label = start.strftime("%B %Y")
    end_label = end.strftime("%B %Y")
    prior_start = start.replace(year=start.year - 1)
    prior_end = end.replace(year=end.year - 1)
    prior_start_label = prior_start.strftime("%B %Y")
    prior_end_label = prior_end.strftime("%B %Y")

    yoy = "n/a" if sc.yoy_pct is None else f"{sc.yoy_pct:+.1f}%"
    peers = "n/a" if sc.peer_avg_yoy_pct is None else f"{sc.peer_avg_yoy_pct:+.1f}%"
    vs_peer = "n/a" if sc.vs_peers_pct is None else f"{sc.vs_peers_pct:+.1f} pts"
    l3 = "n/a" if sc.last_3mo_vs_prior_3mo_pct is None else f"{sc.last_3mo_vs_prior_3mo_pct:+.1f}%"
    l3y = "n/a" if sc.last_3mo_yoy_pct is None else f"{sc.last_3mo_yoy_pct:+.1f}%"

    growing_lines = "\n".join(
        f"  - {format_account_label(a)}: ${a['current']:,.0f} "
        f"(was ${a['prior']:,.0f}, {a['delta']:+,.0f})"
        for a in sc.top_growing_accounts
    ) or "  (none)"
    declining_lines = "\n".join(
        f"  - {format_account_label(a)}: ${a['current']:,.0f} "
        f"(was ${a['prior']:,.0f}, {a['delta']:+,.0f})"
        for a in sc.top_declining_accounts
    ) or "  (none)"
    stale_lines = "\n".join(
        f"  - {format_account_label(a)}: was ${a['prior']:,.0f} ({prior_start_label}–{prior_end_label}), $0 this period"
        for a in sc.stale_accounts
    ) or "  (none)"
    new_lines = "\n".join(
        f"  - {format_account_label(a)}: ${a['current']:,.0f} this period (was $0)"
        for a in sc.new_accounts
    ) or "  (none)"

    pc_lines = "\n".join(
        f"  - {p['desc'] or p['price_class']}: ${p['revenue']:,.0f}, GP%={p['gp_pct']:.1f}%"
        for p in sc.price_class_top
    ) or "  (none)"

    week_block = ""
    if week_lines:
        week_block = (
            f"\nTHIS REP'S WEEKLY CADENCE:\n"
            f"  last_full_week ({week_lines['previous_week_start']} -> "
            f"{week_lines['previous_week_end']}): "
            f"${week_lines['previous_week_revenue']:,.0f} on "
            f"{week_lines['previous_week_lines']:,} lines\n"
            f"  current_week_to_date ({week_lines['current_week_start']} -> "
            f"{week_lines['current_week_end']}): "
            f"${week_lines['current_week_revenue']:,.0f} on "
            f"{week_lines['current_week_lines']:,} lines\n"
        )

    # Include rep tier in the data so the AI understands the context.
    tier_label = "STRUGGLING (bottom 40% + declining/low activation)" if is_struggling else "PERFORMING"

    user_msg = (
        f"REP: {rep_key}  [TIER: {tier_label}]\n"
        f"WINDOW: {start_label} to {end_label} ({start} -> {end}) | "
        f"Prior year same window: {prior_start_label} to {prior_end_label}\n"
        f"Product lines covered: {cc_label}\n\n"
        f"{overview_block}"
        f"REP SCORECARD:\n"
        f"  revenue=${sc.revenue:,.0f} ({start_label}–{end_label}), "
        f"prior=${sc.prior_revenue:,.0f} ({prior_start_label}–{prior_end_label}), yoy={yoy}\n"
        f"  peer_avg_yoy={peers}, vs_peers={vs_peer} (peer set: {sc.peer_count} reps)\n"
        f"  rank_revenue={sc.rank_revenue}, rank_yoy={sc.rank_yoy}\n"
        f"  gp%={sc.gpp_pct:.1f}, lines={sc.invoice_lines}\n"
        f"  total_accounts={sc.total_accounts}, active={sc.active_accounts} "
        f"({sc.active_account_pct:.0f}%)\n"
        f"  accounts_with_core_displays={sc.accounts_with_core_displays} "
        f"({sc.core_display_coverage_pct:.0f}%)\n"
        f"  sample_lines={sc.sample_lines}, samples_per_account={sc.samples_per_account:.2f}\n"
        f"  last_3mo=${sc.last_3mo_revenue:,.0f}, prior_3mo=${sc.prior_3mo_revenue:,.0f}, "
        f"vs_prior_3mo={l3}, yoy_3mo={l3y}\n\n"
        f"TOP PRODUCTS BY REVENUE (what this rep is actually selling):\n{pc_lines}\n\n"
        f"TOP GROWING ACCOUNTS ({start_label}–{end_label} vs {prior_start_label}–{prior_end_label}):\n{growing_lines}\n\n"
        f"TOP DECLINING ACCOUNTS ({start_label}–{end_label} vs {prior_start_label}–{prior_end_label}):\n{declining_lines}\n\n"
        f"STALE ACCOUNTS (had revenue {prior_start_label}–{prior_end_label}, zero {start_label}–{end_label}):\n{stale_lines}\n\n"
        f"NEW ACCOUNTS (zero {prior_start_label}–{prior_end_label}, revenue {start_label}–{end_label}):\n{new_lines}\n"
        f"{week_block}\n"
        f"NOTES:\n  " + ("\n  ".join(sc.notes) if sc.notes else "(none)")
    )
    return sys_msg, user_msg


def _fallback_body(
    sc: RepScorecard,
    period_overview: PeriodOverview | None,
    week_lines: dict | None,
) -> str:
    """Deterministic fallback when AI is not configured or fails.
    Matches the new 5-section structure (scoreboard / win / opportunity /
    coaching insight / focus) so the format is consistent."""
    parts: list[str] = []

    # 1. QUICK SCOREBOARD
    scoreboard: list[str] = []
    if week_lines:
        prev_rev = week_lines.get("previous_week_revenue", 0.0)
        scoreboard.append(f"Last week: ${prev_rev:,.0f}")
    if sc.yoy_pct is not None and not sc.is_yoy_outlier:
        scoreboard.append(f"Period YoY: {sc.yoy_pct:+.1f}%")
    elif sc.last_3mo_vs_prior_3mo_pct is not None:
        scoreboard.append(f"3-month trend: {sc.last_3mo_vs_prior_3mo_pct:+.1f}%")
    if sc.active_accounts and sc.total_accounts:
        scoreboard.append(f"Active accounts: {sc.active_accounts}/{sc.total_accounts} ({sc.active_account_pct:.0f}%)")
    if scoreboard:
        parts.append("QUICK SCOREBOARD\n" + "\n".join(f"• {s}" for s in scoreboard))

    # 2. BIGGEST WIN
    if sc.top_growing_accounts:
        a = sc.top_growing_accounts[0]
        parts.append(
            f"BIGGEST WIN\n"
            f"{format_account_label(a)} is up ${a['delta']:,.0f} vs last year — "
            f"that's ${a['current']:,.0f} in the period vs ${a['prior']:,.0f} prior."
        )
    elif sc.new_accounts:
        a = sc.new_accounts[0]
        parts.append(
            f"BIGGEST WIN\n"
            f"New account activated: {format_account_label(a)} at ${a['current']:,.0f} — "
            f"great prospecting work."
        )

    # 3. BIGGEST OPPORTUNITY
    if sc.stale_accounts:
        a = sc.stale_accounts[0]
        parts.append(
            f"BIGGEST OPPORTUNITY\n"
            f"{format_account_label(a)} bought ${a['prior']:,.0f} last year and "
            f"nothing this period. A call or visit could re-engage them."
        )
    elif sc.top_declining_accounts:
        a = sc.top_declining_accounts[0]
        parts.append(
            f"BIGGEST OPPORTUNITY\n"
            f"{format_account_label(a)} is down ${-a['delta']:,.0f} — "
            f"worth a focused conversation to understand what changed."
        )

    # 4. COACHING INSIGHT
    if sc.core_display_coverage_pct > 0:
        parts.append(
            f"COACHING INSIGHT\n"
            f"{sc.core_display_coverage_pct:.0f}% of your accounts have core displays in place. "
            f"Accounts with strong display presence typically drive more repeat volume — "
            f"it's worth reviewing placements on your larger accounts."
        )
    elif sc.samples_per_account > 0:
        parts.append(
            f"COACHING INSIGHT\n"
            f"Sample activity ({sc.sample_lines} placements) is a strong leading indicator. "
            f"Keep that up — early placements tend to convert into orders in the next 60 days."
        )

    # 5. THIS WEEK'S FOCUS
    if sc.stale_accounts:
        a = sc.stale_accounts[0]
        parts.append(
            f"THIS WEEK'S FOCUS\n"
            f"Reconnect with {format_account_label(a)} — they've gone quiet and have strong history."
        )
    elif sc.top_declining_accounts:
        a = sc.top_declining_accounts[0]
        parts.append(
            f"THIS WEEK'S FOCUS\n"
            f"Have a conversation with {format_account_label(a)} about what's changed."
        )
    elif sc.top_growing_accounts:
        a = sc.top_growing_accounts[0]
        parts.append(
            f"THIS WEEK'S FOCUS\n"
            f"Double down on {format_account_label(a)} — momentum is on your side there."
        )

    parts.append("Full numbers in the scorecard below. Hit reply with any questions.")
    return "\n\n".join(parts)


def _fallback_shoutout(rep_key: str, sc: RepScorecard | None) -> str:
    if sc is None:
        return "Solid effort this week \u2014 keep it up."
    if sc.top_growing_accounts:
        a = sc.top_growing_accounts[0]
        return f"Big win at {format_account_label(a)} (+${a['delta']:,.0f} YoY)."
    if sc.new_accounts:
        a = sc.new_accounts[0]
        return f"Opened up {format_account_label(a)} this period \u2014 great prospecting."
    if sc.yoy_pct is not None and sc.yoy_pct > 0 and not sc.is_yoy_outlier:
        return f"Up {sc.yoy_pct:+.1f}% YoY \u2014 trending the right way."
    if sc.last_3mo_vs_prior_3mo_pct is not None and sc.last_3mo_vs_prior_3mo_pct > 0:
        return f"3-month momentum {sc.last_3mo_vs_prior_3mo_pct:+.1f}% \u2014 nice trend."
    return "Consistent activity this week \u2014 grinding pays off."
