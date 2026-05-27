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

from datetime import datetime
from typing import Callable

import io
import pandas as pd
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
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
from app.data.loaders import load_price_class_lookup, load_rep_assignments
from app.services.manager_analytics import aggregate_for_ai
from app.storage.repos import (
    AIAnalysis,
    delete_ai_analysis,
    find_ai_analysis_by_hash,
    hash_question,
    list_ai_analyses,
    save_ai_analysis,
    set_pinned,
)
from app.ui.theme import ACCENT, BORDER, SURFACE, TEXT, TEXT_MUTED
from app.ui.views._header import ViewHeader
from app.ui.widgets.cards import KpiCard
from app.ui.widgets.sales_filter_bar import SalesFilterBar


SYSTEM_PROMPT = """\
You are an analytical assistant for a sales manager at a flooring distributor.
Your role is to deliver the HIGHEST LEVEL of analysis the data supports — not a summary, not a surface read.
The manager is looking for deep dives, meaningful correlations, and the highest-impact items to act on.

BE BLUNT AND DIRECT. Call out underperformers by name. Name winners specifically. When a trend is bad, say it's bad.
Do not soften, hedge, or add corporate filler. No opening pleasantries, no closing remarks.
Every sentence must either surface an insight, flag a risk, or recommend an action.

DATA SOURCES (trust in this order):
1. PRE-AGGREGATED TABLES — ground truth for all rankings, totals, and comparisons.
2. FULL CSV — use for line-level detail, account-specific trends, or correlation analysis.
Always prefer aggregates for summary questions; use CSV for granular lookups.

ANALYSIS QUALITY RULES:
- Weight your analysis toward LARGE SAMPLE SIZES. A rep with 2 accounts at +40% is not a top performer.
  A rep with 50 accounts at +15% across multiple cost centers is a real signal. Distinguish outliers from trends.
- When you find correlations (e.g. display placements → higher volume), quantify them if the data supports it.
- Don't report what the manager can read from the table — tell them what the table means.
- Flag the SINGLE most actionable insight or area of concern prominently. Lead with highest-impact findings.
- If a number looks anomalous (e.g. one account driving 60% of a rep's YoY swing), call it out and discount it.

FORMATTING RULES — follow these strictly:
- ALWAYS include the time period with every sales figure.
  Say: '$25,239 (Feb–Apr 2025) → $12,548 (Feb–Apr 2026)' — NEVER just '$12,548' without a date range.
- ALWAYS pair account numbers with their name: 'ABC FLOORING (#50342)' or '#50342 · ABC FLOORING'.
  Never cite a bare account number alone.
- ALWAYS use price class/product descriptions, NOT 6-character price class codes.
  Say 'Carpet Residential' not 'CPTRES'. The PRICE CLASS REFERENCE in the data block maps codes → names.
- Format all dollar amounts with $ and thousands separators: $25,239.
- Use GP% for profitability (gross profit as % of revenue).
- When listing reps, use the name from the data; when listing accounts, use name + number.

CLOSED ACCOUNTS:
- Account labels suffixed with '[CLOSED]' are permanently closed accounts.
- Accounts that reopened at the same physical address under a new BACCT# have ALREADY been merged
  into the open account's history automatically — the data reflects the unified customer.
- Never recommend a rep visit, call, or pursue a closed account.
- You MAY reference a closed account to explain a revenue drop or quantify lost territory —
  but every action item must target an OPEN account.

PRIOR-YEAR DATA (year-over-year comparisons):
- The user message ALWAYS includes a 'PRIOR-YEAR SAME WINDOW AGGREGATES' block whenever
  prior-year data has been loaded (the SalesFilterBar's 'Also load prior year' option is
  on by default). That block contains by_rep, by_cc, top_accounts and by_period tables for
  EXACTLY the same calendar window one year earlier.
- For ANY question about year-over-year change, growth vs last year, 'last N days vs same N days
  last year', or 'who grew the most' — USE the PRIOR-YEAR SAME WINDOW aggregates as the
  comparison baseline. Do NOT refuse the question on grounds of 'only the current window is
  provided' if the prior-year block is present.
- If the prior-year block is missing (the user turned the option off), say so plainly and ask the
  user to enable 'Also load prior year' on the filter bar.

PLAIN LANGUAGE:
- Speak the way a real sales manager talks. Avoid jargon like 'whitespace'. Say 'missing
  product category' or 'cross-sell gap' instead.

MARKETING PROGRAMS:
- When the user message includes a 'MARKETING PROGRAMS' block, use it to look for
  correlations between program enrollment (e.g. CCA Buying Group, NRF Rebate Program)
  and rep/account performance metrics already in the aggregates.
- Programs flagged with '*' or listed under 'STARRED PROGRAMS' have been marked as
  important by the manager — prioritise insights about them.
- Correlation is not causation: never claim a program 'caused' growth. Speak in terms
  of 'enrolled accounts are growing X% faster than non-enrolled accounts in scope'.
- Never invent a program name, code, or category not present in the block.

CATEGORY-SCOPED ACCOUNT QUESTIONS — CRITICAL:
- For ANY question that names a marketing category or program (e.g. 'which CCA accounts
  grew', 'list NRF Rebate Program customers that declined', 'top CCA Buying Group
  accounts'), you MUST answer using ONLY accounts that appear in the
  'ACCOUNTS BY MARKETING CATEGORY' block under that exact category heading.
- An account is "in CCA Buying Group" if and only if it is listed under the
  '[CCA Buying Group]' heading in that block. Do NOT include any account that is not
  under that heading, no matter how its name reads. The category heading is the
  authoritative enrollment list.
- Each row in the FULL INVOICED LINES CSV also carries 'marketing_categories' and
  'marketing_program_codes' columns. If you filter the CSV by category, only include
  rows whose 'marketing_categories' column contains that exact category string.
- If the named category has zero matching accounts with activity in scope, say so
  plainly ("No CCA Buying Group accounts have invoiced revenue in the current window").
  Never substitute a non-enrolled account to make the answer non-empty.
"""


# Higher default output limit for the Ask AI view — the manager expects deep analysis,
# not truncated responses. Use at least 4096 tokens; honour any higher config value.
_AI_CHAT_MIN_OUTPUT_TOKENS = 4096


class _AskWorker(QThread):
    answered = Signal(str, dict)
    failed = Signal(str)

    def __init__(self, cfg: AppConfig, system: str, user: str,
                 max_output_tokens: int | None = None) -> None:
        super().__init__()
        self._cfg, self._system, self._user = cfg, system, user
        self._max_tokens = max_output_tokens or max(
            _AI_CHAT_MIN_OUTPUT_TOKENS, cfg.ai.max_output_tokens
        )

    def run(self) -> None:
        try:
            provider = build_provider(self._cfg.ai)
            res = provider.complete(
                [ChatMessage("system", self._system),
                 ChatMessage("user", self._user)],
                model=self._cfg.ai.model,
                max_output_tokens=self._max_tokens,
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
        self._get_db = get_db
        self._df: pd.DataFrame | None = None
        self._prior_df: pd.DataFrame | None = None
        self._workers: list[_AskWorker] = []
        # Enrichment lookups loaded lazily when first ask is made
        self._pc_lookup: dict[str, str] = {}   # price_class_code -> description
        self._acct_lookup: dict[str, dict] = {}  # account_number -> {name, old}
        # Marketing programs cache (loaded once per session in _ask).
        self._mp_types_df = None
        self._mp_placements_df = None

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)

        root.addWidget(
            ViewHeader(
                "Ask the AI",
                "Ask any question about the currently-loaded invoiced sales. "
                "Pick cost centers + dates, press Run, then type your question. "
                "Prior-year same-window data is included automatically for year-over-year questions.",
            )
        )

        # KPI estimates
        kpi_row = QHBoxLayout()
        self.kpi_rows = KpiCard("Rows in scope", "—")
        self.kpi_data_tokens = KpiCard("Est. data tokens", "\u2014", "full dataset sent to AI")
        self.kpi_total_tokens = KpiCard("Est. prompt tokens", "\u2014",
                                        "system + question + full data")
        self.kpi_cost_est = KpiCard("Est. input cost", "\u2014", "gpt-4.1 @ $2/1M tokens")
        for k in (self.kpi_rows, self.kpi_data_tokens, self.kpi_total_tokens, self.kpi_cost_est):
            kpi_row.addWidget(k, 1)
        root.addLayout(kpi_row)

        body = QHBoxLayout()
        body.setSpacing(12)
        self.filter_bar = SalesFilterBar(get_db, cfg=self._cfg, code_prefix_filter="0", page_id="ask_ai")
        self.filter_bar.sales_loaded_with_prior.connect(self._on_loaded)
        body.addWidget(self.filter_bar)

        # Right side: splitter [history | chat]
        self.right_split = QSplitter(Qt.Orientation.Horizontal)

        # ----- history pane
        history_card = QFrame()
        history_card.setObjectName("card")
        history_card.setMinimumWidth(240)
        hv = QVBoxLayout(history_card)
        hv.setContentsMargins(14, 14, 14, 14)
        hv.setSpacing(8)

        h_title_row = QHBoxLayout()
        h_title = QLabel("Saved analyses")
        h_title.setStyleSheet("font-weight: 600;")
        h_title_row.addWidget(h_title)
        h_title_row.addStretch(1)
        self.new_btn = QPushButton("+ New")
        self.new_btn.clicked.connect(self._start_new)
        h_title_row.addWidget(self.new_btn)
        hv.addLayout(h_title_row)

        from PySide6.QtWidgets import QLineEdit
        self.history_search = QLineEdit()
        self.history_search.setPlaceholderText("Search saved Q&A…")
        self.history_search.textChanged.connect(self._refresh_history_filter)
        hv.addWidget(self.history_search)

        self.history_list = QListWidget()
        self.history_list.setAlternatingRowColors(True)
        self.history_list.itemActivated.connect(self._open_history_item)
        self.history_list.itemClicked.connect(self._open_history_item)
        self.history_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.history_list.customContextMenuRequested.connect(self._history_menu)
        hv.addWidget(self.history_list, 1)

        self.history_hint = QLabel("Each Q&A you ask is saved here so you "
                                   "never have to re-ask the same question.")
        self.history_hint.setWordWrap(True)
        self.history_hint.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        hv.addWidget(self.history_hint)

        # ----- chat pane
        chat_pane = QWidget()
        rv = QVBoxLayout(chat_pane)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(8)

        self.similar_banner = QLabel("")
        self.similar_banner.setWordWrap(True)
        self.similar_banner.setVisible(False)
        self.similar_banner.setStyleSheet(
            "background: #FEF3C7; color: #92400E; border: 1px solid #FCD34D; "
            "border-radius: 6px; padding: 8px 10px; font-size: 12px;"
        )
        rv.addWidget(self.similar_banner)

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
        self.input.textChanged.connect(self._on_question_changed)
        rv.addWidget(self.input)

        actions = QHBoxLayout()
        self.ask_btn = QPushButton("Ask")
        self.ask_btn.setProperty("primary", True)
        self.ask_btn.setEnabled(False)
        self.ask_btn.clicked.connect(self._ask)
        actions.addWidget(self.ask_btn)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._start_new)
        actions.addWidget(self.clear_btn)
        actions.addStretch(1)
        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {TEXT_MUTED};")
        actions.addWidget(self.status)
        rv.addLayout(actions)

        self.right_split.addWidget(history_card)
        self.right_split.addWidget(chat_pane)
        self.right_split.setStretchFactor(0, 0)
        self.right_split.setStretchFactor(1, 1)
        self.right_split.setSizes([280, 720])
        body.addWidget(self.right_split, 1)
        root.addLayout(body, 1)

        # Internal state for history rendering
        self._all_history: list[AIAnalysis] = []
        self._refresh_history()

    # --------------------------------------------------------------- data
    def _on_loaded(self, df: pd.DataFrame, prior_df: pd.DataFrame | None = None) -> None:
        self._df = df
        self._prior_df = prior_df if (prior_df is not None and not prior_df.empty) else None
        self.ask_btn.setEnabled(not df.empty)
        self._refresh_token_estimate()

    def _refresh_token_estimate(self) -> None:
        rows = 0 if self._df is None else len(self._df)
        self.kpi_rows.set_value(f"{rows:,}")
        # Full dataset — no cap — is sent to AI.
        data_tok = estimate_df_tokens(self._df) if self._df is not None else 0
        sys_tok = estimate_text_tokens(SYSTEM_PROMPT)
        q_tok = estimate_text_tokens(self.input.toPlainText())
        total = sys_tok + q_tok + data_tok
        self.kpi_data_tokens.set_value(f"{data_tok:,}")
        self.kpi_total_tokens.set_value(f"{total:,}")
        # Rough cost at gpt-4.1 input pricing ($2 per 1M tokens)
        cost_usd = total / 1_000_000 * 2.0
        self.kpi_cost_est.set_value(
            f"~${cost_usd:.3f}" if cost_usd < 0.10 else f"~${cost_usd:.2f}"
        )

    # --------------------------------------------------------------- ask
    def _ask(self) -> None:
        if self._df is None or self._df.empty:
            return
        question = self.input.toPlainText().strip()
        if not question:
            self.status.setText("Type a question first.")
            return

        # --- Lazy-load enrichment lookups (fast queries, done once per session) ---
        if not self._pc_lookup:
            try:
                self._pc_lookup = load_price_class_lookup(self._get_db())
            except Exception:  # noqa: BLE001
                pass
        if not self._acct_lookup:
            try:
                adf = load_rep_assignments(self._get_db())
                if adf is not None and not adf.empty:
                    for row in adf[["account_number", "account_name", "old_account_number"]].drop_duplicates(
                        subset=["account_number"]
                    ).itertuples(index=False):
                        acct = str(row.account_number).strip()
                        name = str(row.account_name or "").strip().lstrip("*").strip()
                        old = str(row.old_account_number or "").strip()
                        if acct:
                            self._acct_lookup[acct] = {"name": name, "old": old}
            except Exception:  # noqa: BLE001
                pass
        if self._mp_types_df is None or self._mp_placements_df is None:
            try:
                from app.data.loaders import (
                    load_marketing_program_placements,
                    load_marketing_program_types,
                )
                self._mp_types_df = load_marketing_program_types(self._get_db())
                self._mp_placements_df = load_marketing_program_placements(self._get_db())
            except Exception:  # noqa: BLE001
                pass

        # Enrich the DataFrame copy: add price_class_desc so the CSV sent to
        # the AI uses descriptions, not 6-char codes.
        df_enriched = self._df.copy()
        if self._pc_lookup and "price_class" in df_enriched.columns:
            df_enriched["price_class_desc"] = (
                df_enriched["price_class"].astype(str).map(
                    lambda c: self._pc_lookup.get(c.strip(), c)
                )
            )

        # Enrich each row with the account's marketing-program categories +
        # starred-program codes so the AI can FILTER the CSV deterministically
        # by questions like "which CCA accounts grew?". Without these columns
        # the model has no per-row way to know which accounts are in CCA and
        # ends up picking arbitrary accounts.
        acct_cat_map: dict[str, set[str]] = {}
        acct_code_map: dict[str, set[str]] = {}
        try:
            from app.services.marketing_programs import account_program_maps
            acct_cat_map, acct_code_map = account_program_maps(
                self._mp_placements_df,
                self._mp_types_df,
                self._cfg.marketing_program_category_by_code,
                self._cfg.marketing_program_starred,
                only_starred=False,
            )
        except Exception:  # noqa: BLE001
            pass
        if acct_cat_map and "account_number" in df_enriched.columns:
            acct_col = df_enriched["account_number"].astype(str).str.strip()
            df_enriched["marketing_categories"] = acct_col.map(
                lambda a: "; ".join(sorted(acct_cat_map.get(a, set()))) or ""
            )
            df_enriched["marketing_program_codes"] = acct_col.map(
                lambda a: ",".join(sorted(acct_code_map.get(a, set()))) or ""
            )

        # Full dataset — no row cap. The aggregate tables handle large datasets
        # efficiently; the raw CSV is also sent in full so the AI can answer
        # line-level questions without losing data.
        buf = io.StringIO()
        df_enriched.to_csv(buf, index=False)
        csv_text = buf.getvalue()

        # Pre-aggregate the FULL filtered dataset (ground truth for rankings).
        agg = aggregate_for_ai(self._df)
        agg_text = _format_aggregates(agg, acct_lookup=self._acct_lookup)

        # Prior-year same window aggregates — included automatically so the AI
        # can answer YoY / "last N days vs same N days last year" questions
        # without needing the user to expand the date range manually.
        prior_agg_text = ""
        if self._prior_df is not None and not self._prior_df.empty:
            try:
                prior_agg = aggregate_for_ai(self._prior_df)
                prior_agg_text = _format_aggregates(prior_agg, acct_lookup=self._acct_lookup)
            except Exception:  # noqa: BLE001
                prior_agg_text = ""

        s, e = self.filter_bar.date_range()
        ccs = self.filter_bar.selected_codes() or ["ALL"]

        # Compute the prior-year same-window date range for the prompt label.
        try:
            prior_s = s.replace(year=s.year - 1)
        except ValueError:
            from datetime import timedelta as _td
            prior_s = s - _td(days=365)
        try:
            prior_e = e.replace(year=e.year - 1)
        except ValueError:
            from datetime import timedelta as _td
            prior_e = e - _td(days=365)

        # Pull in the manager's app-side context so the AI can reason about
        # related sample CCs and core displays even though those rows aren't
        # in the loaded sales DataFrame.
        related_samples: list[str] = []
        related_displays: list[str] = []
        try:
            sel = set(self.filter_bar.selected_codes())
            if sel and self._cfg is not None:
                from app.services.manager_analytics import (
                    normalise_sample_product_pairs,
                )
                # Normalised so direction-of-entry in CC Mapping doesn't matter.
                pairs = normalise_sample_product_pairs(
                    self._cfg.sample_to_product_cc
                )
                related_samples = sorted({
                    s_cc for s_cc, p_cc in pairs.items() if p_cc in sel
                })
                related_displays = sorted({
                    code
                    for cc, codes in self._cfg.core_displays_by_cc.items()
                    if cc in sel
                    for code in codes
                })
        except Exception:  # noqa: BLE001
            pass

        scope_extra = ""
        if related_samples:
            scope_extra += (
                f"Related sample cost centers (samples that feed these "
                f"products): {', '.join(related_samples)}\n"
            )
        if related_displays:
            scope_extra += (
                f"Core display codes for these CCs: "
                f"{', '.join(related_displays)}\n"
            )

        # Build price class reference table so AI can resolve codes → descriptions.
        pc_ref = ""
        if self._pc_lookup:
            pc_ref = "\nPRICE CLASS REFERENCE (code → description):\n"
            pc_ref += "\n".join(
                f"  {code}: {desc}" for code, desc in sorted(self._pc_lookup.items())
            ) + "\n"

        prior_block = ""
        if prior_agg_text:
            prior_block = (
                f"\nPRIOR-YEAR SAME WINDOW AGGREGATES "
                f"({prior_s.isoformat()} to {prior_e.isoformat()} — use these for "
                f"year-over-year comparisons):\n{prior_agg_text}\n"
            )
        else:
            prior_block = (
                "\nPRIOR-YEAR SAME WINDOW AGGREGATES: not loaded — the user has "
                "'Also load prior year' turned OFF on the filter bar. If the question "
                "requires a YoY comparison, tell the user to enable that option and re-run.\n"
            )

        # Marketing-programs context — included automatically so the AI can
        # look for correlations between program enrollment and account/rep
        # performance. Scoped to accounts that appear in the loaded sales
        # DataFrame so we don't drown the prompt in programs outside scope.
        mp_block = ""
        try:
            from app.services.marketing_programs import summarise_for_ai
            scope_accts: set[str] | None = None
            if "account_number" in self._df.columns:
                scope_accts = set(self._df["account_number"].dropna().astype(str).str.strip().unique())
                scope_accts.discard("")
            mp_summary = summarise_for_ai(
                self._mp_placements_df,
                self._mp_types_df,
                self._cfg.marketing_program_category_by_code,
                self._cfg.marketing_program_starred,
                account_filter=scope_accts,
            )
            if mp_summary:
                mp_block = "\n" + mp_summary
        except Exception:  # noqa: BLE001
            mp_block = ""

        # Authoritative per-category × account revenue block. For each
        # marketing category (CCA Buying Group, NRF Rebate Program, …) list
        # EVERY enrolled account that has revenue in scope, with current and
        # prior-year totals + delta. This is the ONLY source of truth for
        # "which <category> accounts grew / shrank" questions — without it
        # the model has no per-account answer and hallucinates.
        mp_accounts_block = ""
        try:
            if acct_cat_map and "account_number" in self._df.columns:
                import pandas as _pd
                cur = (
                    self._df.groupby(
                        self._df["account_number"].astype(str).str.strip()
                    )["revenue"].sum()
                    if "revenue" in self._df.columns else _pd.Series(dtype=float)
                )
                if self._prior_df is not None and not self._prior_df.empty and \
                   "account_number" in self._prior_df.columns and "revenue" in self._prior_df.columns:
                    prior = self._prior_df.groupby(
                        self._prior_df["account_number"].astype(str).str.strip()
                    )["revenue"].sum()
                else:
                    prior = _pd.Series(dtype=float)

                # Build category -> list of (acct, cur, prior)
                cat_to_accts: dict[str, list[tuple[str, float, float]]] = {}
                for acct, cats in acct_cat_map.items():
                    c_rev = float(cur.get(acct, 0.0) or 0.0)
                    p_rev = float(prior.get(acct, 0.0) or 0.0)
                    if c_rev == 0.0 and p_rev == 0.0:
                        continue  # not in scope window
                    for cat in cats:
                        cat_to_accts.setdefault(cat, []).append((acct, c_rev, p_rev))

                if cat_to_accts:
                    lines: list[str] = [
                        "ACCOUNTS BY MARKETING CATEGORY (AUTHORITATIVE — for any "
                        "question asking about a specific marketing category, use "
                        "ONLY these accounts. Do not pick accounts outside this "
                        "list. Current = " + s.isoformat() + " to " + e.isoformat()
                        + "; Prior = " + prior_s.isoformat() + " to " + prior_e.isoformat() + "):"
                    ]
                    from app.services.marketing_programs import UNCATEGORIZED as _UNCAT
                    _MAX_PER_CAT = 50  # cap to bound prompt size; full data still in CSV.
                    for cat in sorted(cat_to_accts.keys()):
                        if cat == _UNCAT:
                            continue  # skip noise; manager hasn't categorised these
                        rows = sorted(cat_to_accts[cat], key=lambda r: r[1], reverse=True)
                        lines.append(f"\n[{cat}] — {len(rows)} enrolled account(s) with activity in scope:")
                        for acct, c_rev, p_rev in rows[:_MAX_PER_CAT]:
                            info = self._acct_lookup.get(acct, {})
                            name = info.get("name", "")
                            old = info.get("old", "")
                            label = (
                                f"{name} (#{old}) [{acct}]" if name and old
                                else f"{name} [{acct}]" if name
                                else f"[{acct}]"
                            )
                            codes = ",".join(sorted(acct_code_map.get(acct, set())))
                            delta = c_rev - p_rev
                            pct = (delta / p_rev * 100.0) if p_rev > 0 else None
                            pct_s = f"{pct:+.1f}%" if pct is not None else "n/a"
                            lines.append(
                                f"  - {label}: ${c_rev:,.0f} cur vs ${p_rev:,.0f} prior "
                                f"({delta:+,.0f}, {pct_s}) — codes: {codes}"
                            )
                        if len(rows) > _MAX_PER_CAT:
                            lines.append(
                                f"  - … and {len(rows) - _MAX_PER_CAT} more (filter on "
                                f"the CSV column 'marketing_categories' for the full list)"
                            )
                    mp_accounts_block = "\n" + "\n".join(lines) + "\n"
        except Exception:  # noqa: BLE001
            mp_accounts_block = ""

        user_msg = (
            f"Date range (invoice date): {s.isoformat()} to {e.isoformat()}\n"
            f"Cost centers in scope: {', '.join(ccs)}\n"
            f"{scope_extra}"
            f"Total rows: {len(df_enriched):,} (full dataset — no truncation).\n\n"
            f"PRE-AGGREGATED TABLES (full dataset \u2014 use these for ranking "
            f"and totals):\n{agg_text}\n"
            f"{prior_block}"
            f"{mp_block}"
            f"{mp_accounts_block}"
            f"{pc_ref}"
            f"Question: {question}\n\n"
            f"FULL INVOICED LINES (CSV \u2014 all {len(df_enriched):,} rows, "
            f"price_class_desc + marketing_categories + marketing_program_codes "
            f"columns added):\n{csv_text}"
        )

        self.transcript.append(
            f"<p style='margin:8px 0;'><b style='color:{ACCENT}'>You:</b> "
            f"{question}</p>"
        )
        self._pending_question = question
        self.input.clear()
        self.status.setText("Asking the model…")
        self.ask_btn.setEnabled(False)

        worker = _AskWorker(self._cfg, SYSTEM_PROMPT, user_msg)
        worker.answered.connect(self._on_answer)
        worker.failed.connect(self._on_failed)
        self._workers.append(worker)
        worker.finished.connect(
            lambda W=worker: self._workers.remove(W) if W in self._workers else None
        )
        worker.start()

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

        # Persist for future reference.
        try:
            s, e = self.filter_bar.date_range()
            ccs = self.filter_bar.selected_codes()
            scope_label = self._scope_label()
            title = self._title_for(self._pending_question)
            save_ai_analysis(
                title=title,
                question=self._pending_question,
                answer=text,
                scope_label=scope_label,
                cost_centers=ccs,
                date_start=s,
                date_end=e,
                rows_in_scope=0 if self._df is None else len(self._df),
                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                total_tokens=int(usage.get("total_tokens", 0) or 0),
                model=self._cfg.ai.model,
            )
            self._refresh_history()
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"{usage_str}  (save failed: {exc})")

    def _on_failed(self, msg: str) -> None:
        self.ask_btn.setEnabled(True)
        self.status.setText(f"Failed — {msg}")

    # --------------------------------------------------------------- helpers
    def _scope_label(self) -> str:
        s, e = self.filter_bar.date_range()
        ccs = self.filter_bar.selected_codes()
        cc_part = "all CCs" if not ccs else f"{len(ccs)} CC(s)"
        return f"{cc_part} · {s.isoformat()} → {e.isoformat()}"

    def _title_for(self, question: str) -> str:
        q = (question or "").strip().splitlines()[0] if question else ""
        return q[:80] or "Untitled analysis"

    def _on_question_changed(self) -> None:
        self._refresh_token_estimate()
        # Surface previously-saved similar question, if any.
        text = self.input.toPlainText().strip()
        if not text:
            self.similar_banner.setVisible(False)
            return
        prior = find_ai_analysis_by_hash(hash_question(text, self._scope_label()))
        if prior:
            self.similar_banner.setText(
                f"You already asked this for this scope on "
                f"{prior.created_at[:10]} — open it from “Saved analyses” "
                f"on the left to skip another API call."
            )
            self.similar_banner.setVisible(True)
        else:
            self.similar_banner.setVisible(False)

    # --------------------------------------------------------------- history
    def _refresh_history(self) -> None:
        self._all_history = list_ai_analyses(limit=300)
        self._refresh_history_filter()

    def _refresh_history_filter(self) -> None:
        needle = self.history_search.text().strip().lower()
        self.history_list.clear()
        for a in self._all_history:
            hay = f"{a.title} {a.question} {a.scope_label}".lower()
            if needle and needle not in hay:
                continue
            star = "★ " if a.pinned else ""
            label = f"{star}{a.title}\n   {a.created_at[:16]} · {a.scope_label}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, a.id)
            self.history_list.addItem(item)

    def _open_history_item(self, item: QListWidgetItem) -> None:
        analysis_id = item.data(Qt.ItemDataRole.UserRole)
        match = next((a for a in self._all_history if a.id == analysis_id), None)
        if not match:
            return
        ans_html = "<br>".join(
            ln.replace("<", "&lt;").replace(">", "&gt;")
            for ln in match.answer.splitlines()
        )
        self.transcript.setHtml(
            f"<p style='color:{TEXT_MUTED}; font-size:11px;'>"
            f"Saved {match.created_at[:16]} · {match.scope_label} · "
            f"{match.rows_in_scope:,} rows · {match.total_tokens:,} tokens "
            f"({match.model})</p>"
            f"<p style='margin:8px 0;'><b style='color:{ACCENT}'>You:</b> "
            f"{match.question.replace(chr(10), '<br>')}</p>"
            f"<p style='margin:8px 0;'><b>Assistant:</b> {ans_html}</p>"
        )
        self.status.setText(f"Restored saved analysis #{match.id}.")

    def _history_menu(self, pos) -> None:
        item = self.history_list.itemAt(pos)
        if not item:
            return
        analysis_id = item.data(Qt.ItemDataRole.UserRole)
        match = next((a for a in self._all_history if a.id == analysis_id), None)
        if not match:
            return
        menu = QMenu(self)
        pin_act = QAction("Unpin" if match.pinned else "Pin to top", menu)
        pin_act.triggered.connect(lambda: (set_pinned(analysis_id, not match.pinned),
                                           self._refresh_history()))
        del_act = QAction("Delete", menu)
        del_act.triggered.connect(lambda: (delete_ai_analysis(analysis_id),
                                           self._refresh_history()))
        menu.addAction(pin_act)
        menu.addSeparator()
        menu.addAction(del_act)
        menu.exec(self.history_list.viewport().mapToGlobal(pos))

    def _start_new(self) -> None:
        self.transcript.setHtml(
            f"<p style='color:{TEXT_MUTED}'>Ask a new question below — your "
            "previous Q&A are saved on the left.</p>"
        )
        self.input.clear()
        self.similar_banner.setVisible(False)
        self.status.setText("")


# --------------------------------------------------------------- helpers
def _format_aggregates(
    agg: dict,
    *,
    acct_lookup: dict[str, dict] | None = None,
) -> str:
    """Render the aggregate_for_ai() output as compact text tables.

    ``acct_lookup`` maps account_number -> {name, old} so the by_account
    section shows human-readable labels instead of bare account numbers.
    """
    lines: list[str] = []
    by_rep = agg.get("by_rep") or []
    by_cc = agg.get("by_cc") or []
    by_account = agg.get("by_account") or []
    by_period = agg.get("by_period") or []

    # Synthesize a totals header from by_rep (every line is in some rep bucket).
    if by_rep:
        tot_rev = sum(float(r.get("revenue", 0) or 0) for r in by_rep)
        tot_gp = sum(float(r.get("gross_profit", 0) or 0) for r in by_rep)
        tot_lines = sum(int(r.get("lines", 0) or 0) for r in by_rep)
        gpp = (tot_gp / tot_rev * 100.0) if tot_rev else 0.0
        n_accounts = len({str(r.get("account_number", "")) for r in by_account})
        lines.append(
            f"TOTALS: revenue=${tot_rev:,.0f} | gp=${tot_gp:,.0f} | "
            f"gp%={gpp:.1f} | lines={tot_lines:,} | reps={len(by_rep):,} | "
            f"accounts_in_top200={n_accounts:,}"
        )

    if by_rep:
        lines.append("\nBY REP (descending revenue, top 100):")
        for r in by_rep[:100]:
            lines.append(
                f"  {str(r.get('rep_key','')):<32}  "
                f"${float(r.get('revenue',0)):>14,.0f}  "
                f"gp ${float(r.get('gross_profit',0)):>12,.0f}  "
                f"lines {int(r.get('lines',0)):>5}  "
                f"accts {int(r.get('accounts',0)):>4}"
            )
    if by_cc:
        lines.append("\nBY COST CENTER (descending revenue):")
        for r in by_cc[:50]:
            lines.append(
                f"  {str(r.get('cost_center','')):<6}  "
                f"${float(r.get('revenue',0)):>14,.0f}  "
                f"gp ${float(r.get('gross_profit',0)):>12,.0f}  "
                f"lines {int(r.get('lines',0)):>5}  "
                f"accts {int(r.get('accounts',0)):>4}"
            )
    if by_account:
        lines.append("\nTOP ACCOUNTS (by revenue, up to 200):")
        for r in by_account[:200]:
            acct_num = str(r.get("account_number", ""))
            info = (acct_lookup or {}).get(acct_num, {})
            name = info.get("name", "")
            old = info.get("old", "")
            # Build label: "Account Name (#old) [new_acct]" or just account_number
            if name and old and old != acct_num:
                label = f"{name} (#{old}) [{acct_num}]"
            elif name:
                label = f"{name} [{acct_num}]"
            else:
                label = acct_num
            lines.append(
                f"  {label:<55}  "
                f"${float(r.get('revenue',0)):>14,.0f}  "
                f"gp ${float(r.get('gross_profit',0)):>12,.0f}  "
                f"lines {int(r.get('lines',0)):>5}"
            )
    if by_period:
        lines.append("\nBY FISCAL PERIOD:")
        for r in by_period:
            lines.append(
                f"  FY{r.get('fiscal_year','')} P{r.get('fiscal_period','')} "
                f"({r.get('fiscal_period_name','')})  "
                f"${float(r.get('revenue',0)):>14,.0f}  "
                f"lines {int(r.get('lines',0)):>5}"
            )
    return "\n".join(lines) if lines else "(no aggregate data)"
