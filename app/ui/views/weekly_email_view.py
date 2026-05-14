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
from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
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
    load_rep_assignments,
)
from app.services.fiscal_calendar import find_period
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
        self._scorecards: dict[str, RepScorecard] = {}
        self._period_overview: PeriodOverview | None = None
        self._drafts: dict[str, dict] = {}
        self._context_loaders: list[_ContextLoader] = []
        self._ai_workers: list[_AIDraftWorker] = []
        self._pending_ai_jobs = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)

        root.addWidget(
            ViewHeader(
                "Weekly Sales Email",
                "AI-coached, manager-style drafts \u2014 one per rep \u2014 plus an "
                "optional master leaderboard email. Outbound stays disabled "
                "until you flip it on in Email settings.",
            )
        )

        body = QHBoxLayout()
        body.setSpacing(12)
        self.filter_bar = SalesFilterBar(get_db, cfg=cfg, code_prefix_filter="0")
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
        self._scorecards = compute_rep_scorecards(
            self._df,
            prior_df=self._prior_df,
            assignments_df=self._assignments_df,
            displays_df=self._displays_df,
            samples_df=self._samples_df,
            core_displays_by_cc=self._cfg.core_displays_by_cc,
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
        cc_label = ", ".join(ccs) if ccs else "all cost centers"

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
        # Anchor weekly windows to the most recent invoice date present in
        # the loaded data — if the manager's date range ended a week or
        # more ago, today's calendar week would be empty and the master
        # email would silently render "no invoiced sales last week".
        anchor = self._anchor_date()
        wk_start, wk_end = previous_week_range(anchor)
        cur_start, cur_end_full = current_week_range(anchor)
        cur_end = min(anchor, cur_end_full)

        per_rep_last = revenue_in_window(self._df, wk_start, wk_end, by="rep")
        per_rep_curr = revenue_in_window(self._df, cur_start, cur_end, by="rep")
        leaderboard = sorted(per_rep_last.items(), key=lambda kv: -kv[1])

        if self._has_ai() and leaderboard:
            shoutouts = self._ai_shoutouts(leaderboard)
        else:
            shoutouts = {rep: _fallback_shoutout(rep, self._scorecards.get(rep))
                         for rep, _ in leaderboard}

        body_html = _render_master_html(
            week_start=wk_start, week_end=wk_end,
            cur_start=cur_start, cur_end=cur_end,
            per_rep_last=per_rep_last, per_rep_curr=per_rep_curr,
            shoutouts=shoutouts, period_overview=self._period_overview,
            anchor=anchor,
        )
        subject = f"Team scoreboard \u2014 week of {wk_start.isoformat()}"
        self._drafts[MASTER_KEY] = {
            "rep_name": "All reps (master leaderboard)",
            "salesman_number": "",
            "to": "",
            "cc": "",
            "subject": subject,
            "body_html": body_html,
            "scorecard": {},
            "week_lines": {},
        }
        existing = self.list.findItems(
            "\u2605 Master leaderboard", Qt.MatchFlag.MatchStartsWith
        )
        if existing:
            for it in existing:
                self.list.takeItem(self.list.row(it))
        item = QListWidgetItem(
            f"\u2605 Master leaderboard  ({wk_start.isoformat()} \u2192 {wk_end.isoformat()})"
        )
        item.setData(Qt.ItemDataRole.UserRole, MASTER_KEY)
        self.list.insertItem(0, item)
        self.list.setCurrentRow(0)
        self.queue_btn.setEnabled(True)

    def _ai_shoutouts(self, leaderboard: list[tuple[str, float]]) -> dict[str, str]:
        provider = build_provider(self._cfg.ai)
        sys_msg = (
            "You are a sales manager preparing a one-line, upbeat shout-out "
            "for each rep on a public team leaderboard. ALWAYS find something "
            "honestly positive to say (largest deal, biggest growing account, "
            "best week, most consistent, etc.) even for reps near the bottom \u2014 "
            "never insult or shame. ONE short sentence per rep, no preamble, "
            "no closing. Output exactly one line per rep in the format: "
            "REP_NAME: shout-out"
        )
        bullets = []
        for rep, rev in leaderboard:
            sc = self._scorecards.get(rep)
            if sc is not None and sc.is_yoy_outlier:
                yoy_part = "YoY=outlier (territory transfer; ignore)"
            elif sc is None or sc.yoy_pct is None:
                yoy_part = "YoY=n/a"
            else:
                yoy_part = f"YoY={sc.yoy_pct:+.1f}%"
            top = (
                format_account_label(sc.top_growing_accounts[0])
                if sc and sc.top_growing_accounts else "n/a"
            )
            l3 = (
                f"3mo={sc.last_3mo_vs_prior_3mo_pct:+.1f}%"
                if sc and sc.last_3mo_vs_prior_3mo_pct is not None else "3mo=n/a"
            )
            bullets.append(
                f"- {rep}: week ${rev:,.0f}; {yoy_part}; {l3}; top growing {top}"
            )
        user_msg = "Reps and last-week numbers:\n" + "\n".join(bullets)
        try:
            res = provider.complete(
                [ChatMessage("system", sys_msg), ChatMessage("user", user_msg)],
                model=self._cfg.ai.model,
                max_output_tokens=self._cfg.ai.max_output_tokens,
                temperature=0.6,
                timeout_seconds=self._cfg.ai.request_timeout_seconds,
            )
            out: dict[str, str] = {}
            for line in (res.text or "").splitlines():
                if ":" in line:
                    name, _, msg = line.partition(":")
                    out[name.strip()] = msg.strip()
            for rep, _ in leaderboard:
                out.setdefault(rep, _fallback_shoutout(rep, self._scorecards.get(rep)))
            return out
        except Exception as exc:  # noqa: BLE001
            log.warning("AI shout-outs failed: %s", exc)
            return {rep: _fallback_shoutout(rep, self._scorecards.get(rep))
                    for rep, _ in leaderboard}

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
        no_email = d['to'] or "(no email on file \u2014 set in Sales Reps)"
        header = (
            f"<div style='color:{TEXT_MUTED};font-size:12px;margin-bottom:8px;'>"
            f"<b>To:</b> {no_email}<br>"
            + (f"<b>Cc:</b> {d['cc']}<br>" if d.get('cc') else "")
            + f"<b>Subject:</b> {d['subject']}</div>"
        )
        self.preview.setHtml(header + d["body_html"])

    def _queue(self) -> None:
        ready = sum(1 for k, d in self._drafts.items() if k != MASTER_KEY and d["to"])
        missing = sum(1 for k, d in self._drafts.items() if k != MASTER_KEY and not d["to"])
        master = "yes" if MASTER_KEY in self._drafts else "no"
        self.preview.setHtml(
            f"<h3>Ready to queue</h3>"
            f"<ul>"
            f"<li>{ready} per-rep draft(s) have a recipient.</li>"
            f"<li>{missing} per-rep draft(s) are missing an email \u2014 set them in Sales Reps.</li>"
            f"<li>Master leaderboard included: <b>{master}</b>.</li>"
            f"</ul>"
            f"<p style='color:{TEXT_MUTED};font-size:11px;'>Outbound sending "
            f"is disabled until you flip it on in Email settings; this button "
            f"will become the actual queue handoff in the next release.</p>"
        )


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
    return overview_html + week_html + body_html + footer


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


def _render_master_html(
    *,
    week_start: date,
    week_end: date,
    cur_start: date,
    cur_end: date,
    per_rep_last: dict[str, float],
    per_rep_curr: dict[str, float],
    shoutouts: dict[str, str],
    period_overview: PeriodOverview | None,
    anchor: date | None = None,
) -> str:
    sorted_last = sorted(per_rep_last.items(), key=lambda kv: -kv[1])
    rows = []
    for i, (rep, rev) in enumerate(sorted_last, 1):
        cur = per_rep_curr.get(rep, 0.0)
        msg = shoutouts.get(rep, "")
        zebra = "#FFFFFF" if i % 2 else "#F8FAFC"
        rows.append(
            f"<tr style='background:{zebra};'>"
            f"<td style='padding:6px 10px;color:#64748B;'>{i}</td>"
            f"<td style='padding:6px 10px;font-weight:600;color:#0F172A;'>{rep}</td>"
            f"<td style='padding:6px 10px;text-align:right;font-variant-numeric:tabular-nums;'>${rev:,.0f}</td>"
            f"<td style='padding:6px 10px;text-align:right;color:#475569;font-variant-numeric:tabular-nums;'>${cur:,.0f}</td>"
            f"<td style='padding:6px 10px;color:#334155;'>{msg}</td>"
            f"</tr>"
        )
    table_rows = "".join(rows) or (
        "<tr><td colspan='5' style='padding:12px;color:#888;'>No invoiced sales last week.</td></tr>"
    )
    overview = ""
    if period_overview is not None and period_overview.revenue:
        overview = (
            f"<p style='font-size:13px;'><b>{period_overview.label} \u2014 company recap:</b> "
            f"${period_overview.revenue:,.0f}"
            + ("" if period_overview.yoy_pct is None
               else f" ({period_overview.yoy_pct:+.1f}% YoY)")
            + f" \u00b7 GP {period_overview.gpp_pct:.1f}% \u00b7 "
            + f"{period_overview.active_reps} active reps \u00b7 "
            + f"{period_overview.active_accounts:,} active accounts.</p>"
        )
    anchor_note = ""
    if anchor and anchor < date.today():
        anchor_note = (
            f"<p style='color:#92400E;font-size:11px;background:#FEF3C7;"
            f"border:1px solid #FCD34D;border-radius:6px;padding:6px 10px;"
            f"display:inline-block;margin:6px 0;'>"
            f"Anchored to last invoice date in scope ({anchor.isoformat()}) "
            f"\u2014 widen the date range to include this calendar week."
            f"</p>"
        )
    return (
        "<p>Team \u2014 here\u2019s the scoreboard for last week. Great work to "
        "everyone on the list \u2014 keep grinding.</p>"
        + overview
        + anchor_note
        + f"<h3 style='margin:14px 0 8px 0;'>Week of {week_start.isoformat()} \u2192 {week_end.isoformat()}</h3>"
        + "<table cellpadding='0' cellspacing='0' "
          "style='border-collapse:collapse;width:100%;font-size:13px;"
          "border:1px solid #E2E8F0;border-radius:6px;overflow:hidden;'>"
        + "<thead><tr style='background:#0F172A;color:#F8FAFC;'>"
          "<th style='padding:8px 10px;text-align:left;'>#</th>"
          "<th style='padding:8px 10px;text-align:left;'>Rep</th>"
          "<th style='padding:8px 10px;text-align:right;'>Last week</th>"
          "<th style='padding:8px 10px;text-align:right;'>Week to date</th>"
          "<th style='padding:8px 10px;text-align:left;'>Shout-out</th>"
          "</tr></thead>"
        + "<tbody>" + table_rows + "</tbody></table>"
        + f"<p style='color:#475569;font-size:12px;margin-top:14px;'>"
          f"Week to date covers {cur_start.isoformat()} \u2192 {cur_end.isoformat()}.</p>"
        + "<p style='color:#64748B;font-size:11px;margin-top:6px;'>"
          "Generated by Sales Assistant. Numbers are invoiced (shipped/billed) "
          "lines from the warehouse \u2014 open orders are not included.</p>"
    )


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
    sys_msg = (
        "You are a senior sales manager at a flooring distributor writing a "
        "weekly coaching email to ONE sales rep. The reader is a busy "
        "salesperson \u2014 keep it tight (200\u2013350 words), human, specific, and "
        f"actionable. Tone: {tone_word}. Always:\n"
        "1. Open with one specific positive (a real number or account they "
        "can be proud of). Never generic praise.\n"
        "2. Then 2\u20133 areas to focus on, ranked by impact. Each must cite a "
        "concrete number from the scorecard or account list \u2014 no vague "
        "platitudes.\n"
        "3. End with 1\u20132 specific action items for next week (e.g. 'visit "
        "account 12345 \u2014 they bought $X last year and zero this year').\n"
        "Hard rules:\n"
        "- Only reference numbers in the data block. Do not invent figures.\n"
        "- When you mention an account, ALWAYS use the rep-friendly label "
        "shown in the data (e.g. '50285 (#1234)' or '50285 (#1234 \u00b7 ABC "
        "FLOORING)') because reps recognise their accounts by the legacy "
        "#-number, not the new account number.\n"
        "- If the rep is up vs peers, celebrate it; if down, frame it as a "
        "challenge with peer context.\n"
        "- Skip the scorecard table at the bottom \u2014 the system appends it.\n"
        "- No subject line, no greeting like 'Hi REP'. Start with the body.\n"
        "- If a metric is unavailable (None / n/a), do not mention it."
    )
    if scorecard.is_yoy_outlier:
        sys_msg += (
            "\n\nIMPORTANT: This rep's YoY % is an outlier (likely caused by "
            "an account-territory transfer, not real performance). DO NOT "
            "frame the email around YoY %. Instead lead with absolute "
            "revenue, GP%, last-3-months momentum, top growing/declining "
            "accounts, and active-account ratio. Mention YoY only as a "
            "factual aside, not as praise or criticism."
        )

    overview_block = ""
    if period_overview is not None:
        po = period_overview
        yoy = "n/a" if po.yoy_pct is None else f"{po.yoy_pct:+.1f}%"
        overview_block = (
            f"COMPANY PERIOD OVERVIEW ({po.label}, {po.start} -> {po.end}):\n"
            f"  total_revenue=${po.revenue:,.0f}, prior=${po.prior_revenue:,.0f}, "
            f"yoy={yoy}, gp%={po.gpp_pct:.1f}, active_reps={po.active_reps}\n\n"
        )

    sc = scorecard
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
        f"  - {format_account_label(a)}: was ${a['prior']:,.0f} prior period, $0 this period"
        for a in sc.stale_accounts
    ) or "  (none)"
    new_lines = "\n".join(
        f"  - {format_account_label(a)}: ${a['current']:,.0f} this period (was $0)"
        for a in sc.new_accounts
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

    user_msg = (
        f"REP: {rep_key}\n"
        f"WINDOW: {start} -> {end} (cost centers: {cc_label})\n\n"
        f"{overview_block}"
        f"REP SCORECARD:\n"
        f"  revenue=${sc.revenue:,.0f}, prior=${sc.prior_revenue:,.0f}, yoy={yoy}\n"
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
        f"TOP GROWING ACCOUNTS (current vs prior):\n{growing_lines}\n\n"
        f"TOP DECLINING ACCOUNTS:\n{declining_lines}\n\n"
        f"STALE ACCOUNTS (had revenue last period, zero this period):\n{stale_lines}\n\n"
        f"NEW ACCOUNTS (zero last period, revenue this period):\n{new_lines}\n"
        f"{week_block}\n"
        f"NOTES:\n  " + ("\n  ".join(sc.notes) if sc.notes else "(none)")
    )
    return sys_msg, user_msg


def _fallback_body(
    sc: RepScorecard,
    period_overview: PeriodOverview | None,
    week_lines: dict | None,
) -> str:
    parts: list[str] = []
    parts.append(f"Hi {sc.rep_name.split()[0] if sc.rep_name else 'team'},")
    if sc.top_growing_accounts:
        a = sc.top_growing_accounts[0]
        parts.append(
            f"Nice work on {format_account_label(a)} \u2014 you're up "
            f"${a['delta']:,.0f} on that account vs last year."
        )
    yoy = sc.yoy_pct
    if (
        yoy is not None
        and sc.peer_avg_yoy_pct is not None
        and not sc.is_yoy_outlier
    ):
        parts.append(
            f"Overall you're at {yoy:+.1f}% YoY vs the peer average of "
            f"{sc.peer_avg_yoy_pct:+.1f}%."
        )
    elif sc.is_yoy_outlier and sc.last_3mo_vs_prior_3mo_pct is not None:
        parts.append(
            f"Your year-over-year swing is unusual (likely a territory "
            f"shift) so let's look at recent momentum instead: last 3 "
            f"months vs prior 3 is {sc.last_3mo_vs_prior_3mo_pct:+.1f}%."
        )
    if sc.top_declining_accounts:
        a = sc.top_declining_accounts[0]
        parts.append(
            f"Biggest opportunity: {format_account_label(a)} is off "
            f"${-a['delta']:,.0f} \u2014 worth a focused conversation."
        )
    if sc.stale_accounts:
        a = sc.stale_accounts[0]
        parts.append(
            f"Heads-up: {format_account_label(a)} bought ${a['prior']:,.0f} "
            "last year and nothing this period. A visit or call could "
            "re-engage them."
        )
    parts.append(
        "Full numbers in the scorecard below. Hit reply with any questions."
    )
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
