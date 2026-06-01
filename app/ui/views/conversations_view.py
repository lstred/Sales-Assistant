"""Conversations view — tracks every AI-originated email thread.

Tabs:
- All Conversations: browse all threads with full message history.
- Needs Review: unanswered rep replies; the AI drafts a data-rich response
  from the conversation history + fresh warehouse data. Manager edits and
  sends with one click.
- Action Items: extracted commitments pending follow-up.
"""

from __future__ import annotations

import logging
import re
from email.utils import parseaddr as _parseaddr
from typing import Callable

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.config.models import AppConfig
from app.notifications.email_client import EmailClient
from app.storage.repos import (
    ActionItem,
    Conversation,
    Message,
    create_conversation_for_inbound,
    delete_rep_fact,
    find_conversation_for_reply,
    list_action_items,
    list_conversations,
    list_messages,
    list_rep_facts,
    record_inbound,
    resolve_action_item,
    save_message,
    set_rep_fact_active,
)
from app.ui.theme import ACCENT, BORDER, DANGER, SURFACE, SURFACE_ALT, TEXT, TEXT_MUTED
from app.ui.views._header import ViewHeader

log = logging.getLogger(__name__)


def _parse_email_address(raw: str) -> str:
    """Extract the bare e-mail address from a raw From/Reply-To header value.

    Handles both ``"Display Name <addr@host>"`` and bare ``"addr@host"`` forms.
    Returns an empty string when parsing fails.
    """
    try:
        _, addr = _parseaddr(raw)
        return addr.strip().lower()
    except Exception:
        return ""


# ================================================================ IMAP poll worker

class _ImapPollWorker(QThread):
    """Fetch unseen IMAP messages and match them to existing threads.

    Emits ``new_conv_ids`` (list of conversation IDs that received a new
    inbound message this poll cycle) so callers can trigger auto-reply.
    """

    found = Signal(int)           # total new messages saved
    new_conv_ids = Signal(object) # list[int] of conv IDs with new messages
    error = Signal(str)
    done = Signal()

    def __init__(self, cfg: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg

    def run(self) -> None:
        try:
            client = EmailClient(self._cfg.email)
            replies = client.fetch_new_replies()
            saved = 0
            new_ids: list[int] = []

            # Build a whitelist set and a reverse email→rep_id map once per cycle.
            whitelist: set[str] = {
                e.strip().lower()
                for e in (self._cfg.email.auto_reply_whitelist or [])
                if e.strip()
            }
            management_set: set[str] = {
                e.strip().lower()
                for e in (self._cfg.email.auto_reply_management_emails or [])
                if e.strip()
            }
            # All eligible senders (reps + management) — combined for the orphan check.
            all_eligible: set[str] = whitelist | management_set
            # cfg.rep_emails: {salesman_number: email} — invert for lookups.
            email_to_rep_id: dict[str, str] = {
                v.strip().lower(): k
                for k, v in (self._cfg.rep_emails or {}).items()
                if v.strip()
            }

            for reply in replies:
                # ── 1. Try to match to an existing thread ────────────────────
                conv = find_conversation_for_reply(
                    reply["in_reply_to"],
                    reply["references"],
                )

                if conv is None:
                    # ── 2. Orphan email — check whitelist ────────────────────
                    # This handles both direct/new emails AND replies to threads
                    # the app doesn't know about yet (e.g. forwarded, or first
                    # contact from a rep who has never received an AI email).
                    from_email = _parse_email_address(reply["from_address"])
                    if not from_email:
                        continue
                    if not all_eligible or from_email not in all_eligible:
                        log.debug(
                            "IMAP: skipping unmatched email from %s — not in whitelist",
                            from_email,
                        )
                        continue
                    # Map to a salesman_number if we can; fall back to the bare
                    # email address so the conversation is still created.
                    rep_id = email_to_rep_id.get(from_email, from_email)
                    log.info(
                        "IMAP: new inbound from whitelisted %s → rep_id=%s",
                        from_email, rep_id,
                    )
                    conv = create_conversation_for_inbound(
                        rep_id=rep_id,
                        subject=reply["subject"],
                        message_id=reply["message_id"],
                        from_address=reply["from_address"],
                        body_text=reply["body_text"],
                        body_html=reply["body_html"],
                        imap_uid=reply["imap_uid"],
                    )
                    if conv is None:
                        continue
                    # Message already persisted inside create_conversation_for_inbound.
                    saved += 1
                    if conv.id not in new_ids:
                        new_ids.append(conv.id)
                    continue

                # ── 3. Known thread — record the inbound ─────────────────────
                result = record_inbound(
                    conversation_id=conv.id,
                    message_id=reply["message_id"],
                    in_reply_to=reply["in_reply_to"],
                    from_address=reply["from_address"],
                    subject=reply["subject"],
                    body_text=reply["body_text"],
                    body_html=reply["body_html"],
                    imap_uid=reply["imap_uid"],
                )
                if result is not None:
                    saved += 1
                    if conv.id not in new_ids:
                        new_ids.append(conv.id)

            self.found.emit(saved)
            if new_ids:
                self.new_conv_ids.emit(new_ids)
        except Exception as exc:  # noqa: BLE001
            log.exception("IMAP poll failed")
            self.error.emit(str(exc))
        finally:
            self.done.emit()


# ================================================================ AI reply worker

class _AiReplyWorker(QThread):
    """Draft a reply using conversation history + fresh warehouse data."""

    draft_ready = Signal(str)
    error = Signal(str)
    done = Signal()

    def __init__(
        self,
        cfg: AppConfig,
        conv: Conversation,
        messages: list[Message],
        get_db: Callable | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._conv = conv
        self._messages = messages
        self._get_db = get_db
        # Populated by _fetch_rep_data for rep-mode prompts so the marketing-
        # programs block can be scoped to the rep's territory.
        self._rep_accounts: set[str] | None = None
        self._rep_account_labels: dict[str, str] = {}

    def run(self) -> None:
        try:
            self.draft_ready.emit(self._generate())
        except Exception as exc:  # noqa: BLE001
            log.exception("AI reply generation failed")
            self.error.emit(str(exc))
        finally:
            self.done.emit()

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _strip_html(html: str) -> str:
        text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _extract_account_numbers(text: str) -> list[str]:
        """Pull bare 4-6 digit account IDs (e.g. #40636)."""
        return list(dict.fromkeys(re.findall(r"#?(\d{4,6})", text)))

    @staticmethod
    def _extract_query_keywords(messages: list) -> list[str]:
        """Extract candidate product/entity keywords from inbound message text.

        Returns a deduplicated list of content words (3+ chars, non-stopword) that
        could be product names, account names, or other domain entities the AI should
        look up.  Used to pre-compute QUERY-MATCHED sections in warehouse data blocks.
        """
        all_text = " ".join(
            (m.body_text or "")
            for m in messages
            if m.direction == "inbound"
        )
        words = re.findall(r'\b[a-zA-Z]{3,}\b', all_text.lower())
        _STOP = {
            "the", "and", "for", "are", "but", "not", "you", "all", "can",
            "had", "her", "was", "one", "our", "out", "day", "get", "has",
            "him", "his", "how", "its", "may", "new", "now", "old", "see",
            "two", "way", "who", "did", "does", "this", "that", "with",
            "have", "from", "they", "will", "been", "more", "when", "each",
            "just", "like", "also", "than", "then", "into", "your", "what",
            "some", "time", "very", "most", "over", "send", "please",
            "total", "sales", "ytd", "reps", "year", "data", "would",
            "could", "should", "hello", "thanks", "thank", "regards",
            "dear", "best", "hope", "help", "need", "want", "know", "show",
            "give", "tell", "make", "look", "come", "good", "much", "many",
            "well", "back", "down", "only", "said", "same", "take", "still",
            "here", "even", "such", "long", "name", "first", "being",
            "those", "never", "under", "while", "where", "after", "other",
            "between", "about", "above", "every", "before", "since",
            "without", "further", "always", "again", "there", "through",
            "product", "products", "price", "amount", "number", "account",
            "fiscal", "report", "email", "reply", "period", "month",
            "quarter", "week", "annual", "manager", "please", "sent",
            "let", "updated", "daily", "lukas", "stred", "rep", "per",
            "sale", "each", "class", "line", "gross", "profit", "revenue",
            "breakdown", "detail", "summary", "numbers", "figures",
        }
        seen: set[str] = set()
        result: list[str] = []
        for w in words:
            if w not in _STOP and w not in seen:
                seen.add(w)
                result.append(w)
        return result[:40]

    @staticmethod
    def _find_matched_products(
        pc_all_codes: list[str],
        pc_lookup: dict[str, str],
        keywords: list[str],
    ) -> list[str]:
        """Return price class codes whose description matches any keyword (case-insensitive substring).

        Results are ordered by how many keywords matched (most-specific first).
        """
        scores: dict[str, int] = {}
        for code in pc_all_codes:
            desc = pc_lookup.get(code, code).lower()
            hits = sum(1 for kw in keywords if kw in desc)
            if hits:
                scores[code] = hits
        return sorted(scores, key=lambda c: -scores[c])

    def _history_text(self) -> str:
        sep = "-" * 60 + "\n"
        parts = []
        for msg in self._messages:
            label = "MANAGER → REP" if msg.direction == "outbound" else "REP → MANAGER"
            raw = msg.body_text or (self._strip_html(msg.body_html) if msg.body_html else "")
            parts.append(f"[{label}  {msg.sent_at[:16]}]\n{raw[:3000].strip()}")
        return "\n\n" + sep.join(parts)

    def _fetch_rep_data(self) -> str:
        """Load this rep's full warehouse dashboard: FY YTD revenue, top products, top accounts.

        Always included in the AI prompt so the model always has real data — even when
        the rep's message contains no explicit account numbers.  Returns an empty string
        on any error so callers never crash.
        """
        if not self._get_db:
            return ""
        try:
            from datetime import date as _date
            import pandas as pd
            from app.data.loaders import (
                load_invoiced_sales,
                load_price_class_lookup,
                load_rep_assignments,
            )
            from app.services.fiscal_calendar import fiscal_year_for, fy_start_date

            db = self._get_db()
            if db is None:
                return ""

            # Current FY YTD and prior FY same window
            today = _date.today()
            fy = fiscal_year_for(today)
            fy_start = fy_start_date(fy)
            cur_start, cur_end = fy_start, today
            # Mirror window in prior FY (same offset from FY start)
            days_offset = (today - fy_start).days
            prior_fy_start = fy_start_date(fy - 1)
            prior_start = prior_fy_start
            prior_end = prior_fy_start + __import__("datetime").timedelta(days=days_offset)

            # Resolve salesman_number from conv.rep_id
            asgn = load_rep_assignments(db)
            if asgn is None or asgn.empty:
                return ""

            rep_id = str(self._conv.rep_id).strip()
            rep_rows = asgn[asgn["salesman_number"].astype(str).str.strip() == rep_id]

            # If not found by number, try reverse email → rep_id map
            if rep_rows.empty:
                email_to_num = {
                    v.strip().lower(): k
                    for k, v in (self._cfg.rep_emails or {}).items()
                    if v.strip()
                }
                num = email_to_num.get(rep_id.lower(), "")
                if num:
                    rep_rows = asgn[asgn["salesman_number"].astype(str).str.strip() == num]

            if rep_rows.empty:
                return ""

            rep_name = str(rep_rows.iloc[0].get("salesman_name", rep_id)).strip()
            rep_accounts: set[str] = set(rep_rows["account_number"].astype(str).str.strip())

            # Account metadata maps
            acct_name_map: dict[str, str] = {}
            old_acct_map: dict[str, str] = {}
            for _, r in rep_rows.iterrows():
                acct = str(r["account_number"]).strip()
                acct_name_map[acct] = str(r.get("account_name", "")).strip().lstrip("*")
                old_acct_map[acct] = str(r.get("old_account_number", "")).strip()

            # Stash on self so other prompt sections (e.g. marketing programs)
            # can scope their data to this rep's accounts.
            self._rep_accounts = rep_accounts
            self._rep_account_labels = dict(acct_name_map)

            # Load invoiced sales (product CCs only — prefix "0")
            df_cur = load_invoiced_sales(db, cur_start, cur_end, code_prefix="0")
            df_prior_full = load_invoiced_sales(db, prior_start, prior_end, code_prefix="0")

            if df_cur is None or df_cur.empty:
                return ""

            df_c = df_cur[df_cur["account_number"].astype(str).str.strip().isin(rep_accounts)].copy()
            df_p: pd.DataFrame
            if df_prior_full is not None and not df_prior_full.empty:
                df_p = df_prior_full[
                    df_prior_full["account_number"].astype(str).str.strip().isin(rep_accounts)
                ].copy()
            else:
                df_p = pd.DataFrame()

            if df_c.empty:
                return ""

            pc_lookup = load_price_class_lookup(db)

            # ── YTD totals ────────────────────────────────────────────────
            ytd_rev: float = df_c["revenue"].sum()
            prior_rev: float = df_p["revenue"].sum() if not df_p.empty else 0.0
            yoy_str = (
                f"{(ytd_rev - prior_rev) / prior_rev * 100:+.1f}% YoY"
                if prior_rev > 0
                else "no prior data"
            )

            lines: list[str] = [
                f"WAREHOUSE DATA FOR {rep_name.upper()} — LIVE FROM DATABASE",
                f"Period: {cur_start.strftime('%b %d, %Y')} → {cur_end.strftime('%b %d, %Y')}  (FY {fy} YTD)",
                f"YTD Revenue:  ${ytd_rev:>12,.0f}",
                f"Prior FY YTD: ${prior_rev:>12,.0f}  ({yoy_str})",
                "",
            ]

            # ── All products (for full catalog + keyword matching) ────────
            pc_all = (
                df_c.groupby("price_class", dropna=False)
                .agg(revenue=("revenue", "sum"), gp=("gross_profit", "sum"))
                .reset_index()
                .sort_values("revenue", ascending=False)
            )
            prior_pc: dict[str, float] = (
                df_p.groupby("price_class")["revenue"].sum().to_dict()
                if not df_p.empty else {}
            )
            all_pc_codes = [str(r).strip() for r in pc_all["price_class"]]

            # ── Query-matched products (pre-computed for this conversation) ─
            keywords = self._extract_query_keywords(self._messages)
            matched_codes = self._find_matched_products(all_pc_codes, pc_lookup, keywords)
            if matched_codes:
                # Full account breakdown for every matched product
                pc_acct_all = (
                    df_c[df_c["price_class"].astype(str).str.strip().isin(matched_codes)]
                    .groupby(["price_class", "account_number"], dropna=False)
                    .agg(revenue=("revenue", "sum"), gp=("gross_profit", "sum"))
                    .reset_index()
                )
                lines.append(
                    "QUERY-MATCHED PRODUCTS — results pre-computed from your question\n"
                    "(USE THIS SECTION FIRST to answer product-specific questions):"
                )
                for pc_code in matched_codes:
                    desc = pc_lookup.get(pc_code, pc_code)
                    sub = (
                        pc_acct_all[pc_acct_all["price_class"].astype(str).str.strip() == pc_code]
                        .sort_values("revenue", ascending=False)
                    )
                    pc_row = pc_all[pc_all["price_class"].astype(str).str.strip() == pc_code]
                    pc_ytd = pc_row["revenue"].sum() if not pc_row.empty else 0.0
                    pc_gp = pc_row["gp"].sum() if not pc_row.empty else 0.0
                    pc_gp_pct = pc_gp / pc_ytd * 100 if pc_ytd > 0 else 0.0
                    pc_prior = prior_pc.get(pc_code, 0.0)
                    pc_yoy = f"{(pc_ytd - pc_prior) / pc_prior * 100:+.1f}% YoY" if pc_prior > 0 else "no prior data"
                    lines.append(
                        f"\n  {desc}\n"
                        f"  FY {fy} YTD: ${pc_ytd:,.0f}  |  Prior FY YTD: ${pc_prior:,.0f}  |  {pc_yoy}  |  GP: {pc_gp_pct:.1f}%"
                    )
                    if not sub.empty:
                        lines.append(f"  {'Account':<34} {'YTD $':>11} {'GP%':>6}")
                        lines.append(f"  {'-'*34} {'-'*11} {'-'*6}")
                        for _, row in sub.iterrows():
                            acct = str(row["account_number"]).strip()
                            name = acct_name_map.get(acct, "")
                            old = old_acct_map.get(acct, "")
                            label = (f"{name} (#{old})" if name and old else name or f"Acct {acct}")[:34]
                            rev = row["revenue"]
                            gp = row["gp"]
                            gp_pct = gp / rev * 100 if rev > 0 else 0.0
                            lines.append(f"  {label:<34} ${rev:>10,.0f} {gp_pct:>5.1f}%")
                lines.append("")

            # ── Top 15 products ───────────────────────────────────────────
            pc_cur_top = pc_all.head(15)
            lines.append(f"TOP PRODUCTS BY REVENUE (FY {fy} YTD vs prior FY YTD):")
            lines.append(f"  {'Product':<32} {'YTD $':>12} {'Prev YTD $':>12} {'YoY':>8} {'GP%':>6}")
            lines.append(f"  {'-'*32} {'-'*12} {'-'*12} {'-'*8} {'-'*6}")
            for _, row in pc_cur_top.iterrows():
                pc = str(row["price_class"]).strip()
                desc = pc_lookup.get(pc, pc)[:32]
                rev = row["revenue"]
                gp = row["gp"]
                gp_pct = gp / rev * 100 if rev > 0 else 0.0
                prev = prior_pc.get(pc, 0.0)
                yoy = f"{(rev - prev) / prev * 100:+.1f}%" if prev > 0 else "new"
                prev_s = f"${prev:,.0f}" if prev > 0 else "—"
                lines.append(f"  {desc:<32} ${rev:>11,.0f} {prev_s:>12} {yoy:>8} {gp_pct:>5.1f}%")
            lines.append("")

            # ── Complete product catalog (all products with revenue) ───────
            lines.append(
                "COMPLETE PRODUCT CATALOG — all products with FY YTD revenue "
                "(search here for any product name not in the top 15 above):"
            )
            lines.append(f"  {'Product Description':<38} {'YTD $':>12} {'GP%':>6}")
            lines.append(f"  {'-'*38} {'-'*12} {'-'*6}")
            for _, row in pc_all.iterrows():
                pc = str(row["price_class"]).strip()
                desc = pc_lookup.get(pc, pc)[:38]
                rev = row["revenue"]
                gp = row["gp"]
                gp_pct = gp / rev * 100 if rev > 0 else 0.0
                lines.append(f"  {desc:<38} ${rev:>11,.0f} {gp_pct:>5.1f}%")
            lines.append("")

            # ── Top accounts ──────────────────────────────────────────────
            acct_cur = (
                df_c.groupby("account_number", dropna=False)
                .agg(revenue=("revenue", "sum"), gp=("gross_profit", "sum"))
                .reset_index()
                .sort_values("revenue", ascending=False)
                .head(20)
            )
            prior_acct: dict[str, float] = (
                df_p.groupby("account_number")["revenue"].sum().to_dict()
                if not df_p.empty
                else {}
            )

            lines.append(f"TOP ACCOUNTS BY REVENUE (FY {fy} YTD):")
            lines.append(f"  {'Account':<34} {'YTD $':>12} {'Prev YTD $':>12} {'YoY':>8}")
            lines.append(f"  {'-'*34} {'-'*12} {'-'*12} {'-'*8}")
            for _, row in acct_cur.iterrows():
                acct = str(row["account_number"]).strip()
                name = acct_name_map.get(acct, "")
                old = old_acct_map.get(acct, "")
                label = (f"{name} (#{old})" if name and old else name or f"Acct {acct}")[:34]
                rev = row["revenue"]
                prev = prior_acct.get(acct, 0.0)
                yoy = f"{(rev - prev) / prev * 100:+.1f}%" if prev > 0 else "new"
                prev_s = f"${prev:,.0f}" if prev > 0 else "—"
                lines.append(f"  {label:<34} ${rev:>11,.0f} {prev_s:>12} {yoy:>8}")

            # ── Product × Account cross-tab (top 20 products) ─────────────
            lines.append("")
            lines.append(
                "PRODUCT × ACCOUNT BREAKDOWN — top 5 accounts per top 20 products\n"
                "(for product-specific questions not covered by QUERY-MATCHED PRODUCTS above):"
            )
            top_pc_codes = all_pc_codes[:20]
            pc_acct_grp = (
                df_c[df_c["price_class"].astype(str).str.strip().isin(top_pc_codes)]
                .groupby(["price_class", "account_number"], dropna=False)
                .agg(revenue=("revenue", "sum"), gp=("gross_profit", "sum"))
                .reset_index()
            )
            for pc_code in top_pc_codes:
                desc = pc_lookup.get(pc_code, pc_code)[:32]
                sub = (
                    pc_acct_grp[pc_acct_grp["price_class"].astype(str).str.strip() == pc_code]
                    .sort_values("revenue", ascending=False)
                    .head(5)
                )
                if sub.empty:
                    continue
                pc_total = sub["revenue"].sum()
                lines.append(f"\n  {desc}  (${pc_total:,.0f} total):")
                for _, row in sub.iterrows():
                    acct = str(row["account_number"]).strip()
                    name = acct_name_map.get(acct, "")
                    old = old_acct_map.get(acct, "")
                    label = (f"{name} (#{old})" if name and old else name or f"Acct {acct}")[:34]
                    rev = row["revenue"]
                    gp = row["gp"]
                    gp_pct = gp / rev * 100 if rev > 0 else 0.0
                    lines.append(f"    {label:<34} ${rev:>10,.0f}  GP {gp_pct:.1f}%")

            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            log.warning("Rep data fetch failed: %s", exc)
            return ""

    def _fetch_benchmark_averages(self) -> str:
        """Company-wide AVERAGES only — no individual rep figures.

        Lets a rep benchmark their own numbers against the company average
        without ever exposing a specific other rep's data. Every figure here is
        an aggregate/average across all active reps. Returns '' on any error so
        the reply flow never blocks.
        """
        if not self._get_db:
            return ""
        try:
            import datetime as _dt

            from app.data.loaders import (
                load_invoiced_sales,
                load_price_class_lookup,
                load_reps,
            )
            from app.services.fiscal_calendar import fiscal_year_for, fy_start_date

            _EXCL = frozenset({"", "house account", "(legacy / pre-aug 2025)"})
            db = self._get_db()
            if db is None:
                return ""

            active_reps_df = load_reps(db)
            active_rep_names: set[str] = set()
            if active_reps_df is not None and not active_reps_df.empty:
                active_rep_names = {
                    str(n).strip().upper()
                    for n in active_reps_df["name"]
                    if str(n).strip()
                }

            def _is_valid_rep(name: str) -> bool:
                n = name.strip()
                if n.lower() in _EXCL:
                    return False
                if active_rep_names:
                    return n.upper() in active_rep_names
                return bool(n)

            today = _dt.date.today()
            fy = fiscal_year_for(today)
            fy_start = fy_start_date(fy)
            df = load_invoiced_sales(db, fy_start, today, code_prefix="0")
            if df is None or df.empty:
                return ""
            df = df[
                df["salesperson_desc"].fillna("").astype(str).str.strip().apply(_is_valid_rep)
            ].copy()
            if df.empty:
                return ""

            df["_rep"] = df["salesperson_desc"].fillna("").astype(str).str.strip()
            rep_rev = df.groupby("_rep")["revenue"].sum()
            rep_rev = rep_rev[rep_rev > 0]
            n_reps = int(rep_rev.shape[0])
            if n_reps == 0:
                return ""

            total_rev = float(df["revenue"].sum())
            total_gp = (
                float(df["gross_profit"].sum()) if "gross_profit" in df.columns else 0.0
            )
            avg_rev = total_rev / n_reps
            median_rev = float(rep_rev.median())
            gp_pct = total_gp / total_rev * 100 if total_rev > 0 else 0.0

            acct_per_rep = df.groupby("_rep")["account_number"].nunique()
            avg_accts = float(acct_per_rep.mean()) if not acct_per_rep.empty else 0.0
            n_accts = int(df["account_number"].nunique())
            avg_rev_per_acct = total_rev / n_accts if n_accts else 0.0

            lines: list[str] = [
                "COMPANY BENCHMARK AVERAGES (all active reps — AVERAGES ONLY; no "
                "individual rep figures are available here or anywhere in this reply):",
                f"Period: {fy_start.strftime('%b %d, %Y')} → {today.strftime('%b %d, %Y')}  (FY {fy} YTD)",
                f"  Active reps:               {n_reps}",
                f"  Avg revenue per rep:       ${avg_rev:,.0f}",
                f"  Median revenue per rep:    ${median_rev:,.0f}",
                f"  Avg accounts per rep:      {avg_accts:,.0f}",
                f"  Avg revenue per account:   ${avg_rev_per_acct:,.0f}",
                f"  Company-wide GP%:          {gp_pct:.1f}%",
            ]

            # Per-product-line averages (company total ÷ rep count) so a rep can
            # benchmark "how does my Win Win compare to the average rep".
            try:
                pc_lookup = load_price_class_lookup(db)
                pc_tot = (
                    df.groupby("price_class", dropna=False)["revenue"]
                    .sum()
                    .sort_values(ascending=False)
                    .head(10)
                )
                if not pc_tot.empty:
                    lines.append("")
                    lines.append(
                        "  AVG PER REP BY TOP PRODUCT LINE (company total ÷ active reps):"
                    )
                    lines.append(f"  {'Product line':<34} {'Avg/Rep':>12}")
                    lines.append(f"  {'-'*34} {'-'*12}")
                    for code, tot in pc_tot.items():
                        desc = str(pc_lookup.get(str(code).strip(), str(code)))[:34]
                        lines.append(f"  {desc:<34} ${tot / n_reps:>10,.0f}")
            except Exception:  # noqa: BLE001
                log.debug("benchmark per-product-line failed", exc_info=True)

            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            log.warning("Benchmark averages fetch failed: %s", exc)
            return ""

    def _fetch_account_detail(self, account_numbers: list[str]) -> str:
        """Period-by-period invoiced sales for specific accounts mentioned in the thread.

        Supplements the rep dashboard above with granular monthly detail when the
        rep or manager explicitly referenced specific account numbers.
        """
        if not account_numbers or not self._get_db:
            return ""
        try:
            from datetime import date as _date
            from app.data.loaders import load_invoiced_sales, load_rep_assignments
            db = self._get_db()
            if db is None:
                return ""
            end = _date.today()
            start = _date(end.year - 1, 1, 1)
            df = load_invoiced_sales(db, start, end)
            if df is None or df.empty:
                return ""
            df = df[df["account_number"].astype(str).isin(account_numbers)].copy()
            if df.empty:
                return ""

            acct_names: dict[str, str] = {}
            try:
                asgn = load_rep_assignments(db)
                if "account_number" in asgn.columns and "account_name" in asgn.columns:
                    for _, row in asgn[
                        asgn["account_number"].astype(str).isin(account_numbers)
                    ].iterrows():
                        acct_names[str(row["account_number"])] = str(row.get("account_name", ""))
            except Exception:  # noqa: BLE001
                pass

            period_col = "fiscal_period_name" if "fiscal_period_name" in df.columns else None
            if period_col is None:
                df["_period"] = df["invoice_date"].dt.to_period("M").astype(str)
                period_col = "_period"

            grp = (
                df.groupby(["account_number", period_col], dropna=False)
                .agg(revenue=("revenue", "sum"), gp=("gross_profit", "sum"))
                .reset_index()
                .sort_values(["account_number", period_col])
            )

            lines: list[str] = ["\nACCOUNT DETAIL (month-by-month):"]
            for acct_num, sub in grp.groupby("account_number"):
                name = acct_names.get(str(acct_num), "")
                label = f"{name} (#{acct_num})" if name else f"#{acct_num}"
                total = sub["revenue"].sum()
                lines.append(f"\n{label}  —  ${total:,.0f} total")
                lines.append(f"  {'Period':<20} {'Revenue':>10} {'GP':>10}")
                lines.append(f"  {'-'*20} {'-'*10} {'-'*10}")
                for _, r in sub.iterrows():
                    lines.append(
                        f"  {str(r[period_col]):<20}"
                        f" ${r['revenue']:>9,.0f}"
                        f" ${r['gp']:>9,.0f}"
                    )
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            log.warning("Account detail fetch failed: %s", exc)
            return ""

    def _fetch_open_orders_data(self, is_management: bool) -> str:  # noqa: C901
        """Load open (un-invoiced) orders and bucket them by expected ship date.

        Returns a structured plain-text block for the AI prompt covering
        the entire open-order pipeline.  Management view shows all reps;
        rep view scopes to the rep's BILLSLMN-assigned accounts.

        NOTE: loads ALL cost-center prefixes (product + sample) so the grand
        total in the prompt matches the actual warehouse total.  Callers
        should never pass a cc_prefix filter here.
        """
        if not self._get_db:
            return ""
        try:
            from datetime import date as _date, timedelta as _td
            from app.data.loaders import (
                load_all_cost_centers,
                load_open_orders,
                load_price_class_lookup,
                load_rep_assignments,
            )

            db = self._get_db()
            if db is None:
                return ""

            # Load ALL open orders — no code_prefix filter so the grand total
            # matches the real warehouse total (product CCs + sample CCs).
            df = load_open_orders(db)
            if df is None or df.empty:
                return ""

            # ── Lookup tables ─────────────────────────────────────────────
            pc_lookup = load_price_class_lookup(db)

            # Cost-center name lookup: code → human-readable name
            # Use load_all_cost_centers so sample CCs and every post-go-live
            # CC (not just XREF-mapped ones) get a friendly name.
            cc_name_map: dict[str, str] = {}
            try:
                cc_df = load_all_cost_centers(db)
                if cc_df is not None and not cc_df.empty:
                    import math as _math
                    for _, r in cc_df.iterrows():
                        code = str(r.get("cost_center", "") or "").strip()
                        name_raw = r.get("cost_center_name", "")
                        # Safely convert SQL NULL / NaN to empty string
                        if name_raw is None:
                            name = ""
                        elif isinstance(name_raw, float) and _math.isnan(name_raw):
                            name = ""
                        else:
                            name = str(name_raw).strip()
                            if name.lower() in ("nan", "none", "na", "<na>", "null", ""):
                                name = ""
                        if code and name:
                            cc_name_map[code] = name
            except Exception:  # noqa: BLE001
                pass

            asgn = load_rep_assignments(db)
            acct_name_map: dict[str, str] = {}
            old_acct_map: dict[str, str] = {}
            acct_rep_map: dict[str, str] = {}
            if asgn is not None and not asgn.empty:
                for _, r in asgn.iterrows():
                    acct = str(r["account_number"]).strip()
                    if acct not in acct_name_map:
                        acct_name_map[acct] = str(r.get("account_name", "")).strip().lstrip("*")
                        old_acct_map[acct] = str(r.get("old_account_number", "")).strip()
                        acct_rep_map[acct] = str(r.get("salesman_name", "")).strip()

            # ── Scope to rep's accounts when in rep mode ──────────────────
            if not is_management:
                rep_id = str(self._conv.rep_id).strip()
                rep_accounts: set[str] | None = None
                if asgn is not None and not asgn.empty:
                    rep_rows = asgn[asgn["salesman_number"].astype(str).str.strip() == rep_id]
                    if rep_rows.empty:
                        email_to_num = {
                            v.strip().lower(): k
                            for k, v in (self._cfg.rep_emails or {}).items()
                            if v.strip()
                        }
                        num = email_to_num.get(rep_id.lower(), "")
                        if num:
                            rep_rows = asgn[asgn["salesman_number"].astype(str).str.strip() == num]
                    if not rep_rows.empty:
                        rep_accounts = set(rep_rows["account_number"].astype(str).str.strip())
                if rep_accounts:
                    df = df[df["account_number"].astype(str).str.strip().isin(rep_accounts)].copy()
                if df.empty:
                    return ""

            # ── Parse ship dates and assign buckets ───────────────────────
            today = _date.today()

            def _parse_ship(val) -> _date | None:
                try:
                    v = int(val)
                    if v <= 10000:
                        return None
                    return _date(v // 10000, (v // 100) % 100, v % 100)
                except Exception:
                    return None

            df["_ship_date"] = df["order_ship_yyyymmdd"].apply(_parse_ship)

            _BUCKET_ORDER = [
                "Overdue",
                "Next 7 days",
                "8–30 days",
                "31–90 days",
                "90+ days",
                "No ship date",
            ]

            def _bucket(d: _date | None) -> str:
                if d is None:
                    return "No ship date"
                if d < today:
                    return "Overdue"
                delta = (d - today).days
                if delta <= 7:
                    return "Next 7 days"
                if delta <= 30:
                    return "8–30 days"
                if delta <= 90:
                    return "31–90 days"
                return "90+ days"

            df["_bucket"] = df["_ship_date"].apply(_bucket)

            # Normalize CC codes: only 3-digit numeric codes (e.g. '010') are
            # real cost centers.  Non-standard ICCTR values like 'TRAY',
            # 'K.SHOWER', 'TT TRAY' exist in the ITEM table but are not
            # valid cost centers — group them all under '' (UNCLASSIFIED).
            import re as _re
            _STD_CC = _re.compile(r'^\d{3}$')

            def _norm_cc(val) -> str:
                cc = str(val).strip() if val is not None else ""
                return cc if _STD_CC.match(cc) else ""

            df["_cc_norm"] = df["cost_center"].apply(_norm_cc)

            def _cc_label(cc: str) -> str:
                """Human-readable label for a normalized CC code."""
                if not cc:
                    return "UNCLASSIFIED"
                return cc_name_map.get(cc, f"CC {cc}")

            grand_total: float = df["open_revenue"].sum()
            if grand_total == 0:
                return ""

            scope_label = "COMPANY-WIDE" if is_management else "REP TERRITORY"
            lines: list[str] = [
                f"OPEN ORDERS / FUTURE SHIPMENTS — {scope_label} (uninvoiced, not yet counted as sales)",
                f"  As of: {today.strftime('%b %d, %Y')}",
                f"  Total open pipeline: ${grand_total:,.0f}  ← USE THIS AS THE GRAND TOTAL",
                "",
                f"  {'Bucket':<28} {'Open $':>12}  {'Lines':>6}",
                f"  {'-'*28} {'-'*12}  {'-'*6}",
            ]

            bucket_summary = (
                df.groupby("_bucket", dropna=False)
                .agg(open_revenue=("open_revenue", "sum"), count=("open_revenue", "count"))
                .reindex([b for b in _BUCKET_ORDER if b in df["_bucket"].values])
            )
            for bkt, brow in bucket_summary.iterrows():
                lines.append(
                    f"  {str(bkt):<28} ${brow['open_revenue']:>11,.0f}  {int(brow['count']):>6}"
                )

            # ── Company-wide BY COST CENTER summary (total pipeline, all dates) ─
            # This is the SOURCE OF TRUTH for any CC-breakdown question.
            # It covers ALL open orders regardless of ship date.
            lines.append("")
            lines.append("BY COST CENTER — TOTAL OPEN PIPELINE (all dates, including orders with no confirmed ship date):")
            lines.append(f"  {'Cost Center':<36} {'Open $':>12}  {'Lines':>6}")
            lines.append(f"  {'-'*36} {'-'*12}  {'-'*6}")
            cc_grp = (
                df.groupby("_cc_norm", dropna=False)
                .agg(open_revenue=("open_revenue", "sum"), count=("open_revenue", "count"))
                .reset_index()
                .sort_values("open_revenue", ascending=False)
            )
            cc_total: float = 0.0
            for _, row in cc_grp.iterrows():
                cc = str(row["_cc_norm"]).strip()
                name = _cc_label(cc)
                rev = float(row["open_revenue"]) if row["open_revenue"] == row["open_revenue"] else 0.0
                cnt = int(row["count"])
                cc_total += rev
                lines.append(f"  {name[:36]:<36} ${rev:>11,.0f}  {cnt:>6}")
            lines.append(f"  {'TOTAL':<36} ${cc_total:>11,.0f}")

            lines.append("")

            # ── Per-day breakdown for next 14 days (covers 'tomorrow', 'this week') ──
            # This is the EXACT source for any 'shipping on <date>' question.
            lines.append(
                "SHIPPING SCHEDULE — DAY-BY-DAY (use this for any specific-date question):"
            )
            lines.append(f"  {'Date':<24} {'Open $':>12}  {'Lines':>6}")
            lines.append(f"  {'-'*24} {'-'*12}  {'-'*6}")
            for offset in range(0, 15):  # today + next 14 days
                day = today + _td(days=offset)
                day_rows = df[df["_ship_date"] == day]
                day_rev = day_rows["open_revenue"].sum() if not day_rows.empty else 0.0
                day_cnt = int(len(day_rows))
                if offset == 0:
                    label = f"Today {day.strftime('%a %b %d')}"
                elif offset == 1:
                    label = f"Tomorrow {day.strftime('%a %b %d')}"
                else:
                    label = day.strftime("%a %b %d, %Y")
                lines.append(
                    f"  {label[:24]:<24} ${day_rev:>11,.0f}  {day_cnt:>6}"
                )
            lines.append("")

            # ── Per-day × cost-center detail for next 7 days (mgmt only) ──
            if is_management:
                lines.append(
                    "SHIPPING SCHEDULE — NEXT 7 DAYS BY COST CENTER (confirmed ship-date orders only):"
                )
                any_day_printed = False
                for offset in range(0, 8):
                    day = today + _td(days=offset)
                    day_rows = df[df["_ship_date"] == day]
                    if day_rows.empty:
                        continue
                    any_day_printed = True
                    day_total = day_rows["open_revenue"].sum()
                    if offset == 0:
                        day_label = f"Today {day.strftime('%a %b %d')}"
                    elif offset == 1:
                        day_label = f"Tomorrow {day.strftime('%a %b %d')}"
                    else:
                        day_label = day.strftime("%a %b %d, %Y")
                    lines.append(f"  {day_label}  (${day_total:,.0f}):")
                    day_cc = (
                        day_rows.groupby("_cc_norm", dropna=False)["open_revenue"]
                        .sum()
                        .sort_values(ascending=False)
                    )
                    for cc_n, rev in day_cc.items():
                        name = _cc_label(str(cc_n).strip())
                        lines.append(f"    {name[:36]:<36} ${rev:>11,.0f}")
                if not any_day_printed:
                    lines.append("  (No orders have a confirmed ship date in the next 7 days)")
                    lines.append("  Use the BY COST CENTER table above for the full open pipeline.")
                lines.append("")

            # ── Per-bucket detail ─────────────────────────────────────────
            for bkt in _BUCKET_ORDER:
                sub = df[df["_bucket"] == bkt].copy()
                if sub.empty:
                    continue
                bkt_total: float = sub["open_revenue"].sum()
                lines.append(f"{bkt}  (${bkt_total:,.0f}):")

                if is_management:
                    # Rep-level breakdown
                    sub["_rep"] = sub["account_number"].astype(str).str.strip().map(
                        lambda a: acct_rep_map.get(a, "—")
                    )
                    rep_grp = (
                        sub.groupby("_rep", dropna=False)["open_revenue"]
                        .sum()
                        .sort_values(ascending=False)
                        .head(20)
                    )
                    lines.append(f"  {'Rep':<30} {'Open $':>12}")
                    lines.append(f"  {'-'*30} {'-'*12}")
                    for rep_n, rev in rep_grp.items():
                        lines.append(f"  {str(rep_n):<30} ${rev:>11,.0f}")

                    # Cost-center breakdown per bucket
                    cc_bkt = (
                        sub.groupby("_cc_norm", dropna=False)["open_revenue"]
                        .sum()
                        .sort_values(ascending=False)
                    )
                    lines.append(f"  {'Cost Center':<36} {'Open $':>10}")
                    lines.append(f"  {'-'*36} {'-'*10}")
                    for cc_n, rev in cc_bkt.items():
                        name = _cc_label(str(cc_n).strip())
                        lines.append(f"  {name[:36]:<36} ${rev:>9,.0f}")
                else:
                    # Account-level breakdown (rep view)
                    acct_grp = (
                        sub.groupby("account_number", dropna=False)["open_revenue"]
                        .sum()
                        .sort_values(ascending=False)
                        .head(15)
                    )
                    lines.append(f"  {'Account':<36} {'Open $':>10}")
                    lines.append(f"  {'-'*36} {'-'*10}")
                    for acct, rev in acct_grp.items():
                        acct_s = str(acct).strip()
                        name = acct_name_map.get(acct_s, "")
                        old = old_acct_map.get(acct_s, "")
                        label = (
                            f"{name} (#{old})" if name and old
                            else name or f"Acct {acct_s}"
                        )[:36]
                        lines.append(f"  {label:<36} ${rev:>9,.0f}")

                # Product breakdown (both modes, top 8)
                pc_grp = (
                    sub.groupby("price_class", dropna=False)["open_revenue"]
                    .sum()
                    .sort_values(ascending=False)
                    .head(8)
                )
                if not pc_grp.empty:
                    lines.append(f"  {'Product':<36} {'Open $':>10}")
                    lines.append(f"  {'-'*36} {'-'*10}")
                    for pc, rev in pc_grp.items():
                        desc = pc_lookup.get(str(pc).strip(), str(pc).strip())[:36]
                        lines.append(f"  {desc:<36} ${rev:>9,.0f}")
                lines.append("")

            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            log.warning("Open orders data fetch failed: %s", exc)
            return ""

    def _is_management_sender(self) -> bool:
        """Return True when the email thread was initiated by a management sender.

        Management senders are listed in ``cfg.email.auto_reply_management_emails``.
        We check (in order): the conversation ``rep_id`` (when it is an email), and
        then the From: address of every inbound message in the thread.
        """
        mgmt: set[str] = {
            e.strip().lower()
            for e in (self._cfg.email.auto_reply_management_emails or [])
            if e.strip()
        }
        if not mgmt:
            return False
        if "@" in self._conv.rep_id and self._conv.rep_id.lower() in mgmt:
            return True
        for msg in self._messages:
            if msg.direction == "inbound":
                addr = _parse_email_address(msg.from_address or "").lower()
                if addr and addr in mgmt:
                    return True
        return False

    def _fetch_management_data(self) -> str:
        """Load company-wide warehouse dashboard for a management query.

        Unlike ``_fetch_rep_data`` which scopes to a single rep's accounts, this
        loads ALL invoiced sales across ALL reps and territories so the manager can
        ask cross-rep questions (e.g. "which rep sells the most Win Win?").
        Returns an empty string on any error so callers never crash.
        """
        if not self._get_db:
            return ""
        try:
            import datetime as _dt
            import pandas as pd
            from app.data.loaders import (
                load_invoiced_sales,
                load_price_class_lookup,
                load_rep_assignments,
                load_reps,
            )
            from app.services.fiscal_calendar import fiscal_year_for, fy_start_date

            # Names that should never appear in management reports even if present in data.
            _EXCL = frozenset({"", "house account", "(legacy / pre-aug 2025)"})

            db = self._get_db()
            if db is None:
                return ""

            # Build whitelist of current active salespeople from dbo.SALESMAN.
            # Only reps whose name appears in the current SALESMAN table are shown
            # in any per-rep breakdown — this excludes former employees whose
            # accounts in BILLSLMN have not yet been reassigned.
            active_reps_df = load_reps(db)
            active_rep_names: set[str] = set()
            if active_reps_df is not None and not active_reps_df.empty:
                active_rep_names = {
                    str(n).strip().upper()
                    for n in active_reps_df["name"]
                    if str(n).strip()
                }

            def _is_valid_rep(name: str) -> bool:
                n = name.strip()
                if n.lower() in _EXCL:
                    return False
                # If we have an active roster, enforce it; otherwise allow all.
                if active_rep_names:
                    return n.upper() in active_rep_names
                return bool(n)

            today = _dt.date.today()
            fy = fiscal_year_for(today)
            fy_start = fy_start_date(fy)
            cur_start, cur_end = fy_start, today
            days_offset = (today - fy_start).days
            prior_fy_start = fy_start_date(fy - 1)
            prior_start = prior_fy_start
            prior_end = prior_fy_start + _dt.timedelta(days=days_offset)

            df_c = load_invoiced_sales(db, cur_start, cur_end, code_prefix="0")
            df_p_full = load_invoiced_sales(db, prior_start, prior_end, code_prefix="0")

            if df_c is None or df_c.empty:
                return ""
            df_p = df_p_full if (df_p_full is not None and not df_p_full.empty) else pd.DataFrame()

            # Apply active-rep whitelist: remove rows attributed to former/excluded reps
            # so they never surface in any per-rep breakdown.
            df_c = df_c[
                df_c["salesperson_desc"].fillna("").astype(str).str.strip().apply(_is_valid_rep)
            ].copy()
            if not df_p.empty:
                df_p = df_p[
                    df_p["salesperson_desc"].fillna("").astype(str).str.strip().apply(_is_valid_rep)
                ].copy()

            if df_c.empty:
                return ""

            pc_lookup = load_price_class_lookup(db)

            ytd_rev: float = df_c["revenue"].sum()
            prior_rev: float = df_p["revenue"].sum() if not df_p.empty else 0.0
            yoy_str = (
                f"{(ytd_rev - prior_rev) / prior_rev * 100:+.1f}% YoY"
                if prior_rev > 0 else "no prior data"
            )

            lines: list[str] = [
                "COMPANY-WIDE WAREHOUSE DATA — ALL REPS / ALL TERRITORIES",
                f"Period: {cur_start.strftime('%b %d, %Y')} → {cur_end.strftime('%b %d, %Y')}  (FY {fy} YTD)",
                f"Total Company Revenue: ${ytd_rev:>12,.0f}",
                f"Prior FY YTD:          ${prior_rev:>12,.0f}  ({yoy_str})",
                "",
            ]

            # ── All products (for catalog + keyword matching) ─────────────
            pc_all = (
                df_c.groupby("price_class", dropna=False)
                .agg(revenue=("revenue", "sum"), gp=("gross_profit", "sum"))
                .reset_index()
                .sort_values("revenue", ascending=False)
            )
            prior_pc: dict[str, float] = (
                df_p.groupby("price_class")["revenue"].sum().to_dict()
                if not df_p.empty else {}
            )
            all_pc_codes = [str(r).strip() for r in pc_all["price_class"]]

            # ── Query-matched products (pre-computed for this conversation) ─
            keywords = self._extract_query_keywords(self._messages)
            matched_codes = self._find_matched_products(all_pc_codes, pc_lookup, keywords)
            if matched_codes:
                pc_rep_all = (
                    df_c[df_c["price_class"].astype(str).str.strip().isin(matched_codes)]
                    .groupby(["price_class", "salesperson_desc"], dropna=False)
                    .agg(revenue=("revenue", "sum"), gp=("gross_profit", "sum"))
                    .reset_index()
                )
                lines.append(
                    "QUERY-MATCHED PRODUCTS — results pre-computed from your question\n"
                    "(USE THIS SECTION FIRST to answer product-specific questions):"
                )
                for pc_code in matched_codes:
                    desc = pc_lookup.get(pc_code, pc_code)
                    sub = (
                        pc_rep_all[pc_rep_all["price_class"].astype(str).str.strip() == pc_code]
                        .sort_values("revenue", ascending=False)
                    )
                    pc_row = pc_all[pc_all["price_class"].astype(str).str.strip() == pc_code]
                    pc_ytd = pc_row["revenue"].sum() if not pc_row.empty else 0.0
                    pc_gp = pc_row["gp"].sum() if not pc_row.empty else 0.0
                    pc_gp_pct = pc_gp / pc_ytd * 100 if pc_ytd > 0 else 0.0
                    pc_prior = prior_pc.get(pc_code, 0.0)
                    pc_yoy = f"{(pc_ytd - pc_prior) / pc_prior * 100:+.1f}% YoY" if pc_prior > 0 else "no prior data"
                    lines.append(
                        f"\n  {desc}\n"
                        f"  FY {fy} YTD: ${pc_ytd:,.0f}  |  Prior FY YTD: ${pc_prior:,.0f}  |  {pc_yoy}  |  GP: {pc_gp_pct:.1f}%"
                    )
                    if not sub.empty:
                        lines.append(f"  {'Rep Name':<28} {'YTD $':>11} {'GP%':>6}")
                        lines.append(f"  {'-'*28} {'-'*11} {'-'*6}")
                        for _, row in sub.iterrows():
                            rep_n = str(row["salesperson_desc"]).strip() or "(unassigned)"
                            rev = row["revenue"]
                            gp = row["gp"]
                            gp_pct = gp / rev * 100 if rev > 0 else 0.0
                            lines.append(f"  {rep_n:<28} ${rev:>10,.0f} {gp_pct:>5.1f}%")
                lines.append("")

            # ── Sales by rep ──────────────────────────────────────────────
            rep_cur = (
                df_c.groupby("salesperson_desc", dropna=False)
                .agg(revenue=("revenue", "sum"), gp=("gross_profit", "sum"))
                .reset_index()
                .sort_values("revenue", ascending=False)
                .head(30)
            )
            prior_rep_map: dict[str, float] = (
                df_p.groupby("salesperson_desc")["revenue"].sum().to_dict()
                if not df_p.empty else {}
            )

            lines.append(f"SALES BY REP (FY {fy} YTD vs prior FY YTD):")
            lines.append(f"  {'Rep Name':<28} {'YTD $':>12} {'Prev YTD $':>12} {'YoY':>8} {'GP%':>6}")
            lines.append(f"  {'-'*28} {'-'*12} {'-'*12} {'-'*8} {'-'*6}")
            for _, row in rep_cur.iterrows():
                rep_n = str(row["salesperson_desc"]).strip() or "(unassigned)"
                rev = row["revenue"]
                gp = row["gp"]
                gp_pct = gp / rev * 100 if rev > 0 else 0.0
                prev = prior_rep_map.get(rep_n, 0.0)
                yoy = f"{(rev - prev) / prev * 100:+.1f}%" if prev > 0 else "new"
                prev_s = f"${prev:,.0f}" if prev > 0 else "—"
                lines.append(f"  {rep_n:<28} ${rev:>11,.0f} {prev_s:>12} {yoy:>8} {gp_pct:>5.1f}%")
            lines.append("")

            # ── Top 15 products company-wide ──────────────────────────────
            pc_cur_top = pc_all.head(15)
            lines.append(f"TOP PRODUCTS COMPANY-WIDE (FY {fy} YTD):")
            lines.append(f"  {'Product':<32} {'YTD $':>12} {'Prev YTD $':>12} {'YoY':>8} {'GP%':>6}")
            lines.append(f"  {'-'*32} {'-'*12} {'-'*12} {'-'*8} {'-'*6}")
            for _, row in pc_cur_top.iterrows():
                pc = str(row["price_class"]).strip()
                desc = pc_lookup.get(pc, pc)[:32]
                rev = row["revenue"]
                gp = row["gp"]
                gp_pct = gp / rev * 100 if rev > 0 else 0.0
                prev = prior_pc.get(pc, 0.0)
                yoy = f"{(rev - prev) / prev * 100:+.1f}%" if prev > 0 else "new"
                prev_s = f"${prev:,.0f}" if prev > 0 else "—"
                lines.append(f"  {desc:<32} ${rev:>11,.0f} {prev_s:>12} {yoy:>8} {gp_pct:>5.1f}%")
            lines.append("")

            # ── Complete product catalog (all products with revenue) ───────
            lines.append(
                "COMPLETE PRODUCT CATALOG — all products with FY YTD revenue "
                "(search here for any product not in the top 15 above):"
            )
            lines.append(f"  {'Product Description':<38} {'YTD $':>12} {'GP%':>6}")
            lines.append(f"  {'-'*38} {'-'*12} {'-'*6}")
            for _, row in pc_all.iterrows():
                pc = str(row["price_class"]).strip()
                desc = pc_lookup.get(pc, pc)[:38]
                rev = row["revenue"]
                gp = row["gp"]
                gp_pct = gp / rev * 100 if rev > 0 else 0.0
                lines.append(f"  {desc:<38} ${rev:>11,.0f} {gp_pct:>5.1f}%")
            lines.append("")

            # ── Product × Rep cross-tab (top 20 products) ─────────────────
            lines.append("PRODUCT × REP BREAKDOWN — top 5 reps per top 20 products:")
            top_pc_codes = all_pc_codes[:20]
            pc_rep_grp = (
                df_c[df_c["price_class"].astype(str).str.strip().isin(top_pc_codes)]
                .groupby(["price_class", "salesperson_desc"], dropna=False)
                .agg(revenue=("revenue", "sum"), gp=("gross_profit", "sum"))
                .reset_index()
            )
            for pc_code in top_pc_codes:
                desc = pc_lookup.get(pc_code, pc_code)[:32]
                sub = (
                    pc_rep_grp[pc_rep_grp["price_class"].astype(str).str.strip() == pc_code]
                    .sort_values("revenue", ascending=False)
                    .head(5)
                )
                if sub.empty:
                    continue
                pc_total = sub["revenue"].sum()
                lines.append(f"\n  {desc}  (${pc_total:,.0f} total):")
                for _, row in sub.iterrows():
                    rep_n = str(row["salesperson_desc"]).strip() or "(unassigned)"
                    rev = row["revenue"]
                    gp = row["gp"]
                    gp_pct = gp / rev * 100 if rev > 0 else 0.0
                    lines.append(f"    {rep_n:<28} ${rev:>10,.0f}  GP {gp_pct:.1f}%")

            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            log.warning("Management data fetch failed: %s", exc)
            return ""

    def _fetch_marketing_programs_block(self, *, account_filter: set[str] | None) -> str:
        """Return a compact marketing-programs context block for the AI prompt.

        When ``account_filter`` is given (rep mode), counts scope to those
        accounts; otherwise (management mode) it covers the whole company.
        Returns ``""`` on any error so the prompt never breaks.
        """
        if not self._get_db:
            return ""
        try:
            from app.data.loaders import (
                load_marketing_program_placements,
                load_marketing_program_types,
            )
            from app.services.marketing_programs import (
                category_to_accounts_lines,
                per_account_program_lines,
                summarise_for_ai,
            )
            db = self._get_db()
            if db is None:
                return ""
            placements = load_marketing_program_placements(db)
            programs = load_marketing_program_types(db)
            summary = summarise_for_ai(
                placements,
                programs,
                self._cfg.marketing_program_category_by_code,
                self._cfg.marketing_program_starred,
                account_filter=account_filter,
            )
            if not summary:
                return ""
            # Authoritative per-category account list — prevents the AI from
            # guessing membership when asked questions like "which CCA
            # accounts ...". Scoped to rep accounts in rep mode, full org in
            # management mode.
            cat_accts = category_to_accounts_lines(
                placements,
                programs,
                self._cfg.marketing_program_category_by_code,
                self._cfg.marketing_program_starred,
                account_filter=account_filter,
                account_labels=(self._rep_account_labels if account_filter else None),
            )
            if cat_accts:
                summary += "\n" + cat_accts
            # In rep mode also list which of the rep's accounts hold starred
            # programs (when there are any).
            if account_filter:
                lines = per_account_program_lines(
                    placements,
                    programs,
                    self._cfg.marketing_program_category_by_code,
                    self._cfg.marketing_program_starred,
                    account_filter,
                    account_labels=self._rep_account_labels,
                    only_starred=True,
                )
                if lines:
                    summary += (
                        "STARRED-PROGRAM ENROLLMENT BY ACCOUNT (this rep's territory only):\n"
                        + lines
                    )
            return summary
        except Exception:  # noqa: BLE001
            log.exception("marketing programs fetch failed")
            return ""

    def _extract_and_save_facts(
        self,
        rep_id: str,
        last_body: str,
        inbound: list,
    ) -> None:
        """Detect durable facts the rep asserted and persist them (auditable).

        Runs a small, cheap AI call that reads the rep's latest message and
        returns a JSON array of facts (e.g. an account is closed).  Each fact
        is saved to the rep_facts table so future emails and replies honor it.
        Manager senders never create rep facts.  Fails open — never blocks the
        reply flow.
        """
        if not rep_id or not last_body.strip():
            return
        if self._is_management_sender():
            return
        try:
            import json

            from app.ai.base import ChatMessage
            from app.ai.factory import build_provider
            from app.storage.repos import save_rep_fact

            source_message_id = None
            for m in reversed(inbound):
                if getattr(m, "id", None):
                    source_message_id = m.id
                    break
            conv_id = getattr(self._conv, "id", None)

            provider = build_provider(self._cfg.ai)
            system_msg = (
                "You extract durable, actionable FACTS that a sales rep states about their "
                "accounts in an email. Only extract facts that should change how we treat an "
                "account going forward — NOT questions, requests, or pleasantries.\n\n"
                "Return ONLY a JSON array (no prose). Each element:\n"
                '  {"account_number": "<digits or empty>", "account_label": "<name or empty>", '
                '"fact_type": "account_closed|account_note|preference|other", '
                '"fact_text": "<concise restatement of the fact>"}\n\n'
                "Rules:\n"
                "- 'account_closed': the rep says an account is closed, out of business, or no "
                "longer a customer.\n"
                "- 'account_note': a durable status about an account (changed buyer, switched "
                "supplier, on credit hold, seasonal, etc.).\n"
                "- 'preference': how the rep wants to be communicated with or what they care about.\n"
                "- 'other': any other durable fact worth remembering.\n"
                "- Extract the account NUMBER if the rep gives one (4-6 digits).\n"
                "- If the message contains NO durable facts, return exactly: []\n"
                "- Never fabricate an account number or fact not stated by the rep."
            )
            user_msg = (
                f"REP MESSAGE:\n{last_body[:2000]}\n\n"
                "Return the JSON array of durable facts now:"
            )
            result = provider.complete(
                [
                    ChatMessage(role="system", content=system_msg),
                    ChatMessage(role="user", content=user_msg),
                ],
                model=self._cfg.ai.model,
                max_output_tokens=512,
                temperature=0.0,
                timeout_seconds=self._cfg.ai.request_timeout_seconds,
            )
            raw = (result.text or "").strip()
            # Strip code fences if the model wrapped the JSON.
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw).strip()
            # Isolate the JSON array.
            start = raw.find("[")
            endp = raw.rfind("]")
            if start == -1 or endp == -1 or endp <= start:
                return
            facts = json.loads(raw[start : endp + 1])
            if not isinstance(facts, list):
                return
            for f in facts:
                if not isinstance(f, dict):
                    continue
                fact_text = str(f.get("fact_text", "")).strip()
                if not fact_text:
                    continue
                acct = str(f.get("account_number", "") or "").strip() or None
                ftype = str(f.get("fact_type", "note") or "note").strip()
                if ftype not in ("account_closed", "account_note", "preference", "other"):
                    ftype = "other"
                save_rep_fact(
                    rep_id=rep_id,
                    fact_text=fact_text,
                    fact_type=ftype,
                    account_number=acct,
                    account_label=str(f.get("account_label", "") or "").strip(),
                    source="rep_feedback",
                    source_message_id=source_message_id,
                    conversation_id=conv_id,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("Fact extraction failed (non-fatal): %s", exc)

    def _validate_draft(self, draft: str, warehouse_block: str, rep_question: str = "") -> str:
        """Second AI pass: fact-check every number in the draft against source data.

        Runs a fresh AI call at temperature 0.0 instructed only to verify and
        correct — not to add new content.  Falls back to the original draft on
        any error so the auto-reply flow never blocks.
        """
        if not draft.strip() or not warehouse_block.strip():
            return draft
        try:
            from app.ai.base import ChatMessage
            from app.ai.factory import build_provider

            provider = build_provider(self._cfg.ai)
            system_msg = (
                "You are a FACT-CHECKER for an AI sales data assistant.\n"
                "Your ONLY job is to verify that every number (dollar amounts, "
                "percentages, order counts) AND every named entity (cost-center names, "
                "product names, rep names) in the DRAFT EMAIL can be directly traced "
                "to the SOURCE DATA block.\n\n"
                "VERIFICATION PROCESS:\n"
                "1. Extract every number from the draft.\n"
                "2. Find each one in the source data — exact match OR provable sum "
                "of visible rows.\n"
                "3. If a number CANNOT be verified: it is FABRICATED. Remove or "
                "correct it using only what the source data shows.\n"
                "4. If an entire section is fabricated (e.g. a cost-center table "
                "with no matching data): replace it with what the data ACTUALLY "
                "shows, or state the breakdown was not available.\n"
                "5. CHECK COST CENTER NAMES: any name in a cost-center table MUST "
                "appear verbatim in the 'BY COST CENTER' table of the source data. "
                "Reject and replace generic category names like 'Carpet Residential', "
                "'Tile & Stone', 'Wood Flooring', 'Adhesives & Accessories', or "
                "'Other' — these are HALLUCINATIONS unless they appear verbatim in "
                "the source.\n"
                "6. CHECK TOTALS: the TOTAL in the draft MUST equal the TOTAL row "
                "in the source data exactly. If it does not, the breakdown is wrong.\n\n"
                "7. CHECK RELEVANCE: confirm the draft actually ANSWERS what the "
                "reader asked (see THE READER'S REQUEST below). If the draft "
                "ignores, only partially answers, or drifts off-topic, fix it so it "
                "directly addresses the request using only the source data. If the "
                "source data does not contain what was asked, the draft must say so "
                "plainly (do not pad with unrelated figures).\n"
                "8. CHECK COMPLETENESS NOTE: if any relevant data appears missing, the "
                "draft must end with a line starting '⚠ Note:' describing what may be "
                "missing. Add it if it is warranted and absent.\n\n"
                "OUTPUT RULES — STRICTLY FOLLOW:\n"
                "- If the draft is 100% accurate: return it VERBATIM, unchanged.\n"
                "- If corrections are needed: return ONLY the corrected text — no "
                "commentary, no 'CORRECTED:' labels, no explanations.\n"
                "- Do NOT invent any number or name not present in the source data.\n"
                "- Preserve the tone, structure, and formatting of the original.\n"
                "- The GRAND TOTAL in the source data is authoritative — never "
                "contradict it."
            )
            # Trim source data to fit safely — first 7 000 chars covers the
            # open-orders + invoiced-sales summary (the hallucination-prone parts).
            source_excerpt = warehouse_block[:7000]
            question_block = (
                f"THE READER'S REQUEST (the draft must answer THIS):\n{rep_question[:1500]}\n\n"
                if rep_question.strip()
                else ""
            )
            user_msg = (
                f"DRAFT EMAIL TO VERIFY:\n{draft}\n\n"
                f"{question_block}"
                f"SOURCE DATA (ground truth — all valid numbers come from here):\n"
                f"{source_excerpt}\n\n"
                "Return the verified/corrected draft now:"
            )
            result = provider.complete(
                [
                    ChatMessage(role="system", content=system_msg),
                    ChatMessage(role="user", content=user_msg),
                ],
                model=self._cfg.ai.model,
                max_output_tokens=max(1024, self._cfg.ai.max_output_tokens),
                temperature=0.0,
                timeout_seconds=self._cfg.ai.request_timeout_seconds,
            )
            validated = (result.text or "").strip()
            return validated if validated else draft
        except Exception as exc:  # noqa: BLE001
            log.warning("Draft validation failed — using original: %s", exc)
            return draft

    def _generate(self) -> str:
        from app.ai.base import ChatMessage
        from app.ai.factory import build_provider

        provider = build_provider(self._cfg.ai)

        inbound = [m for m in self._messages if m.direction == "inbound"]
        last_body = ""
        if inbound:
            last = inbound[-1]
            last_body = last.body_text or (self._strip_html(last.body_html) if last.body_html else "")

        # Extract & persist any durable facts the rep just asserted (e.g.
        # "that account is closed") BEFORE building the prompt, so the reply
        # can acknowledge the fact and future emails honor it.  Best-effort.
        rep_id = (self._conv.rep_id or "").strip()
        try:
            self._extract_and_save_facts(rep_id, last_body, inbound)
        except Exception:  # noqa: BLE001
            log.debug("fact extraction failed", exc_info=True)

        # Always load the full rep dashboard from the warehouse.
        is_mgmt = self._is_management_sender()

        # Track data-completeness gaps so the reply can flag missing data.
        completeness_notes: list[str] = []

        if is_mgmt:
            # Management sender — load company-wide data across all reps.
            warehouse_data = self._fetch_management_data()
            all_text = " ".join(
                (m.body_text or self._strip_html(m.body_html or "")) for m in self._messages
            )
            account_numbers = self._extract_account_numbers(all_text)
            account_detail = self._fetch_account_detail(account_numbers)
            open_orders = self._fetch_open_orders_data(is_management=True)
            mp_block = self._fetch_marketing_programs_block(account_filter=None)
            if not warehouse_data:
                completeness_notes.append("company-wide sales summary could not be loaded")
            if account_numbers and not account_detail:
                completeness_notes.append(
                    "per-account detail for the referenced account(s) could not be loaded"
                )
            warehouse_block = "\n\n".join(
                x for x in [warehouse_data, account_detail, open_orders, mp_block] if x
            )
        else:
            # Rep sender — scope to this rep's territory only.
            rep_data = self._fetch_rep_data()
            all_text = " ".join(
                (m.body_text or self._strip_html(m.body_html or "")) for m in self._messages
            )
            account_numbers = self._extract_account_numbers(all_text)
            account_detail = self._fetch_account_detail(account_numbers)
            open_orders = self._fetch_open_orders_data(is_management=False)
            mp_block = self._fetch_marketing_programs_block(
                account_filter=self._rep_accounts
            )
            # Company-wide AVERAGES only (no individual rep figures) so the rep
            # can benchmark themselves without seeing another rep's specifics.
            benchmark_block = self._fetch_benchmark_averages()
            if not rep_data:
                completeness_notes.append("your territory sales summary could not be loaded")
            if account_numbers and not account_detail:
                completeness_notes.append(
                    "per-account detail for the referenced account(s) could not be loaded"
                )
            warehouse_block = "\n\n".join(
                x for x in [rep_data, account_detail, open_orders, mp_block, benchmark_block] if x
            )

        # Durable rep-asserted facts (closed accounts, supplier switches, etc.).
        rep_facts = ""
        try:
            from app.storage.repos import rep_facts_block as _rfb
            rep_facts = _rfb(rep_id) if rep_id else ""
        except Exception:  # noqa: BLE001
            log.debug("rep_facts_block lookup failed", exc_info=True)
        if rep_facts:
            warehouse_block = (warehouse_block + "\n\n" + rep_facts).strip() if warehouse_block else rep_facts


        if is_mgmt:
            system_msg = (
                "You are an AI data assistant automatically responding to a MANAGER's email query.\n"
                "You have full access to COMPANY-WIDE warehouse data across ALL reps and territories.\n"
                "ALWAYS use the live data provided — NEVER invent or estimate any number.\n\n"

                "TERMINOLOGY — understand what common terms mean in this business:\n"
                "- 'Product' or 'product line': a PRICE CLASS (e.g. 'WIN WIN BROADLOOM COMMERCIAL'). "
                "Each unique product in the database has a description in the PRODUCT CATALOG.\n"
                "- 'Win Win': matches any product description containing 'win win'. "
                "Always search by partial name — users never use exact database descriptions.\n"
                "- 'Rep' or 'salesperson': a sales representative listed in SALES BY REP.\n"
                "- 'Account' or 'customer': a flooring dealer. Old account numbers like #1234 are what reps know.\n"
                "- 'YTD': fiscal year to date (fiscal year starts Feb 1).\n"
                "- 'CC': cost center / product category (e.g. 'Carpet Residential').\n"
                "- 'GP' = gross profit dollars. 'GP%' = gross profit percentage.\n\n"

                "HOW TO FIND PRODUCTS — follow this order every time:\n"
                "1. Check QUERY-MATCHED PRODUCTS first — this section is pre-computed from the "
                "exact conversation text. It already contains the correct data. USE IT.\n"
                "2. If QUERY-MATCHED PRODUCTS has no data for the question, scan COMPLETE PRODUCT "
                "CATALOG using case-insensitive substring matching (e.g. 'win win' matches "
                "'WIN WIN BROADLOOM', 'WIN WIN COMMERCIAL BROADLOOM', 'WIN WIN RESIDENTIAL', etc.).\n"
                "3. If multiple product descriptions match (e.g. residential AND commercial variants), "
                "show ALL of them in one table AND provide a combined total.\n"
                "4. Only AFTER searching both sections above should you report a product as not found.\n\n"

                "ABSOLUTE RULES — violating any of these is a failure:\n"
                "- NEVER invent, estimate, or fabricate any number. Every figure must come from the warehouse block.\n"
                "- NEVER say 'CLARIFICATION NEEDED' for a product name lookup. Product names can always "
                "be matched by partial name from the COMPLETE PRODUCT CATALOG. Reserve "
                "'CLARIFICATION NEEDED' ONLY for genuinely ambiguous date ranges, rep names with "
                "multiple matches, or requests for data the warehouse cannot provide at all.\n"
                "- Always cite the exact date range covered (e.g. 'Feb 1, 2026 – May 20, 2026 (FY 2027 YTD)').\n"
                "- You CAN and SHOULD compare reps by name — that is appropriate for manager queries.\n\n"

                "OPEN ORDERS / FUTURE SHIPMENTS:\n"
                "- The data block has an 'OPEN ORDERS / FUTURE SHIPMENTS' section with live pipeline data.\n"
                "- These are UN-INVOICED orders — NOT yet counted as sales. Always label them as pipeline.\n"
                "- TWO key sections — use the right one for each question:\n"
                "  1. 'BY COST CENTER — TOTAL OPEN PIPELINE': shows ALL open orders across ALL dates.\n"
                "     Use this when asked 'what's in the pipeline by CC', 'what are we shipping'\n"
                "     or any general shipping/CC breakdown question. This is the COMPLETE picture.\n"
                "  2. 'SHIPPING SCHEDULE — DAY-BY-DAY': shows orders with a CONFIRMED ship date set.\n"
                "     Use this for specific-date questions ('tomorrow', 'Thursday', 'May 22').\n"
                "     IMPORTANT: many orders may have no confirmed ship date — if a day shows $0\n"
                "     or a small amount, it means few orders have that exact date confirmed in the ERP,\n"
                "     NOT that nothing is shipping. Always show the total pipeline context alongside.\n"
                "  3. 'SHIPPING SCHEDULE — NEXT 7 DAYS BY COST CENTER': confirmed orders by CC per day.\n"
                "     Use when asked 'by cost center for Thursday' etc.\n"
                "- For 'shipping [day] by cost center' questions:\n"
                "  (a) Report the confirmed day total from DAY-BY-DAY.\n"
                "  (b) Show the BY COST CENTER breakdown from the TOTAL PIPELINE table.\n"
                "  (c) Add a note: 'Note: $X confirmed with ship date [day]; full open pipeline by CC is shown above.'\n\n"

                "COST CENTER NAMES — CRITICAL:\n"
                "- The ONLY valid cost-center names are those listed in the 'BY COST CENTER' table.\n"
                "- NEVER invent generic flooring category names like 'Carpet Residential',\n"
                "  'Tile & Stone', 'Wood Flooring', 'Adhesives & Accessories', or 'Other'.\n"
                "- Use the EXACT cost-center names from the data — e.g. 'CARPET RESIDENTIAL',\n"
                "  'CUSHION', 'CARPET COMMERCIAL BL', 'CERAMIC', 'COMMERCIAL RESILIENT', 'VCT',\n"
                "  'RESILIENT LVT', 'HARDWOOD', 'UNFINISHED WOOD', 'CARPET LVT', 'SUPPLY LVT',\n"
                "  'POWDER', 'ADHESIVE', 'SHOWER', 'SUNDRIES', 'UNCLASSIFIED', etc. — whatever the data shows.\n"
                "- Show EVERY cost center with non-zero values — do not collapse small CCs into 'Other'.\n"
                "- The TOTAL row in your reply MUST equal the TOTAL row in the data exactly.\n"
                "- 'UNCLASSIFIED' in the data means orders for items without a standard cost center\n"
                "  code — include this row if it appears, labeled as 'UNCLASSIFIED'.\n\n"

                "PROFESSIONAL FORMATTING RULES:\n"
                "- Lead with the direct answer (the table or figure). Context after, not before.\n"
                "- Use ASCII tables with a header row, dash separator, and aligned data columns.\n"
                "- Left-align names/descriptions, right-align all currency and percentage columns.\n"
                "- Include a TOTAL row when showing a multi-row breakdown.\n"
                "- 300 words maximum. If the answer is a table, the table IS the response.\n"
                "- Plain text only — no markdown, no asterisks, no bullet symbols.\n"
                "- Do NOT add a sign-off or closing pleasantry.\n"
                "- End with ONE specific follow-up offer the warehouse can actually deliver.\n\n"

                "CLOSED ACCOUNTS — CRITICAL:\n"
                "- Account labels suffixed with '[CLOSED]' are permanently closed accounts.\n"
                "- Closed accounts that reopened under a new BACCT# at the same address have\n"
                "  ALREADY been merged into the open account's history automatically — the\n"
                "  data you see already reflects the unified customer.\n"
                "- Never instruct the rep to call, visit, or pursue a closed account.\n"
                "- You MAY mention a closed account to explain a revenue drop or quantify lost\n"
                "  territory — but recommendations must always target OPEN accounts.\n\n"

                "MARKETING PROGRAMS:\n"
                "- When a 'MARKETING PROGRAMS' block is present, use it to look for correlations\n"
                "  between program enrollment and performance (revenue, growth, retention).\n"
                "- Programs marked with a leading '*' or listed under 'STARRED PROGRAMS' have\n"
                "  been flagged as important by the manager — prioritise insights about them.\n"
                "- You may answer at the high-level category (e.g. 'CCA Buying Group') OR at\n"
                "  the specific program code level. Never invent a program or category not\n"
                "  present in the block.\n\n"

                "KNOWN FACTS / DATA COMPLETENESS — CRITICAL:\n"
                "- If a 'KNOWN FACTS FROM THE REP' block is present, HONOR it. Do not contradict\n"
                "  a stated fact and do not re-raise an account the rep already told us is closed.\n"
                "- Make sure you actually answer what was asked. If the data needed to fully\n"
                "  answer the question is NOT in the warehouse block, do NOT guess — say which\n"
                "  part you could not cover.\n"
                "- If ANY relevant data may be missing or incomplete, end the reply with a single\n"
                "  line beginning exactly with '⚠ Note:' that states plainly what may be missing\n"
                "  (e.g. '⚠ Note: some data may be incomplete — per-account detail for #51149\n"
                "  could not be loaded; figures above cover only what was retrieved.')."
            )
        else:
            system_msg = (
                "You are an AI data assistant automatically responding to a sales rep's email "
                "on behalf of the sales manager. You have live warehouse data pulled directly from "
                "the sales database — always use it. NEVER invent any number.\n\n"

                "DATA SCOPE & PRIVACY — CRITICAL, NON-NEGOTIABLE:\n"
                "- This rep may ONLY receive SPECIFIC figures for THEIR OWN territory: their "
                "accounts, their products, their orders. All specific data in the warehouse "
                "block is already scoped to this rep — never present account-level or "
                "order-level detail for anyone else.\n"
                "- If the rep asks for ANOTHER individual rep's specific numbers (by name, "
                "territory, 'the top rep', 'rep X', 'who sells the most', a ranking of named "
                "reps, etc.), you MUST politely decline: explain you can only share their own "
                "territory detail, then offer the COMPANY BENCHMARK AVERAGES instead.\n"
                "- The rep CAN ask about COMPANY-WIDE AVERAGES or BENCHMARKS (e.g. 'how do I "
                "compare to the average rep', 'what's the average GP%', 'average sales per "
                "account'). Answer those ONLY from the 'COMPANY BENCHMARK AVERAGES' block, "
                "which contains aggregate averages and NEVER names or breaks out any "
                "individual rep. Never reverse-engineer or estimate a specific rep's number "
                "from an average.\n"
                "- When you cite a benchmark, you MAY compare it to this rep's own figure "
                "(both are allowed) — e.g. 'Your GP% is 31% vs the 28% company average.'\n\n"

                "FUTURE-EMAIL PREFERENCES:\n"
                "- If the rep tells you what they want to see in their WEEKLY/MONTHLY summary "
                "email going forward (e.g. 'always include my top declining accounts'), "
                "acknowledge in ONE sentence that you've noted it and it will be applied to "
                "future emails. (It is saved automatically — you do not need to do anything else.)\n\n"

                "TERMINOLOGY — understand what common terms mean in this business:\n"
                "- 'Product' or 'product line': a PRICE CLASS with a description (e.g. 'WIN WIN BROADLOOM').\n"
                "- Reps use informal product names. 'Win Win' matches any product description containing "
                "'win win'. Always do case-insensitive partial matching.\n"
                "- 'Account' or 'customer': a flooring dealer. Old account numbers like #1234 are standard.\n"
                "- 'YTD': fiscal year to date (fiscal year starts Feb 1).\n"
                "- 'GP' = gross profit dollars. 'GP%' = gross profit percentage.\n\n"

                "HOW TO FIND PRODUCTS — follow this order every time:\n"
                "1. Check QUERY-MATCHED PRODUCTS first — pre-computed from this conversation. USE IT.\n"
                "2. If not there, scan COMPLETE PRODUCT CATALOG with case-insensitive substring matching.\n"
                "3. If multiple variants match, show ALL with a combined total.\n"
                "4. For product-by-account questions: use QUERY-MATCHED PRODUCTS or "
                "PRODUCT × ACCOUNT BREAKDOWN — whichever has the data.\n\n"

                "ABSOLUTE RULES — violating any of these is a failure:\n"
                "- NEVER invent, estimate, or fabricate any number.\n"
                "- NEVER say 'CLARIFICATION NEEDED' for a product name. Always search partial names "
                "from COMPLETE PRODUCT CATALOG. Reserve 'CLARIFICATION NEEDED' ONLY for genuinely "
                "ambiguous requests where data cannot be found even with partial matching.\n"
                "- If the rep asks for data 'by rep' for all reps: you do NOT share other "
                "reps' specific numbers. Point them to the COMPANY BENCHMARK AVERAGES and "
                "deliver their own product × account table.\n"
                "- Always cite the exact date range (e.g. 'Feb 1 – May 20, 2026 FY YTD').\n"
                "- Address the rep by first name once, at the start.\n\n"

                "OPEN ORDERS / FUTURE SHIPMENTS:\n"
                "- The data block includes an 'OPEN ORDERS / FUTURE SHIPMENTS' section with live pipeline.\n"
                "- These are UN-INVOICED orders — pipeline, not sales revenue yet. Label them accordingly.\n"
                "- 'BY COST CENTER — TOTAL OPEN PIPELINE': ALL open orders regardless of ship date.\n"
                "  Use this for general shipping/backlog/CC breakdown questions.\n"
                "- 'SHIPPING SCHEDULE — DAY-BY-DAY': only orders with a confirmed ship date set.\n"
                "  If a day shows $0, it means no orders have that exact date confirmed — NOT that nothing ships.\n"
                "  Always show the total pipeline context alongside any day-specific figures.\n"
                "- If the rep asks 'what's shipping this week', 'do I have any open orders', 'what's my backlog',\n"
                "  or anything about pending / upcoming / future orders — answer from this section.\n\n"

                "COST CENTER NAMES — CRITICAL:\n"
                "- The ONLY valid cost-center names are those listed in the 'BY COST CENTER' table.\n"
                "- NEVER invent generic category names like 'Carpet Residential', 'Tile & Stone',\n"
                "  'Wood Flooring', or 'Other'. Use EXACT names from the data.\n"
                "- Show EVERY cost center with non-zero values — do not collapse small CCs into 'Other'.\n\n"

                "PROFESSIONAL FORMATTING RULES:\n"
                "- Lead with the direct answer (table or figure), then 1-2 sentences of context.\n"
                "- ASCII tables: header row, dash separator line, aligned columns. "
                "Left-align names, right-align numbers. Include totals row where relevant.\n"
                "- 100–220 words maximum. Plain text only — no markdown, no asterisks.\n"
                "- Do NOT add a sign-off or closing pleasantry.\n"
                "- End with ONE specific follow-up offer the warehouse can actually deliver.\n\n"

                "CLOSED ACCOUNTS — CRITICAL:\n"
                "- Account labels suffixed with '[CLOSED]' are permanently closed.\n"
                "- Closed accounts that reopened under a new BACCT# at the same address have\n"
                "  ALREADY been merged into the open account's history — the data already\n"
                "  reflects the unified customer.\n"
                "- Never tell the rep to call, visit, or re-engage a closed account.\n"
                "- You may mention a closed account to explain lost business, but recommend\n"
                "  open accounts where the rep can make up the volume.\n\n"

                "MARKETING PROGRAMS:\n"
                "- When a 'MARKETING PROGRAMS' block is present, use it to spot correlations\n"
                "  between program enrollment and the rep's account performance. Programs\n"
                "  marked with '*' or under 'STARRED PROGRAMS' were flagged as important by\n"
                "  the manager — mention them when relevant. Never invent a program name or\n"
                "  category not present in the block.\n\n"

                "KNOWN FACTS / DATA COMPLETENESS — CRITICAL:\n"
                "- If a 'KNOWN FACTS FROM THE REP' block is present, HONOR it. Never contradict\n"
                "  a fact the rep already told us, and never re-raise an account they said is closed.\n"
                "- Make sure you actually answer what the rep asked. If the data needed is NOT in\n"
                "  the warehouse block, do NOT guess — say which part you could not cover.\n"
                "- If ANY relevant data may be missing or incomplete, end the reply with a single\n"
                "  line beginning exactly with '⚠ Note:' stating plainly what may be missing."
            )

        sender_label = "MANAGER QUERY" if is_mgmt else f"Rep: {self._conv.rep_name or self._conv.rep_id}"
        user_msg = (
            f"{sender_label}\n"
            f"Subject: {self._conv.subject}\n\n"
            f"CONVERSATION HISTORY:\n{self._history_text()}\n\n"
            f"LATEST MESSAGE:\n{last_body[:1500]}\n"
        )
        if warehouse_block:
            data_label = "COMPANY-WIDE WAREHOUSE DATA" if is_mgmt else "FRESH WAREHOUSE DATA (rep's territory)"
            user_msg += f"\n{data_label} (use this — do not invent anything outside it):\n{warehouse_block}\n"
        else:
            user_msg += (
                "\nWARNING: Warehouse data could not be loaded. "
                "Do NOT invent any numbers. Report the data pull failed and "
                "ask them to try again in a few minutes.\n"
            )
        if completeness_notes:
            joined = "; ".join(completeness_notes)
            user_msg += (
                f"\nDATA COMPLETENESS WARNING — some data could not be retrieved: {joined}. "
                "You MUST end your reply with a line starting '⚠ Note:' telling the reader "
                "exactly what may be missing so they don't treat the figures as complete.\n"
            )
        user_msg += "\nDraft the reply now:"

        result = provider.complete(
            [
                ChatMessage(role="system", content=system_msg),
                ChatMessage(role="user", content=user_msg),
            ],
            model=self._cfg.ai.model,
            max_output_tokens=max(1024, self._cfg.ai.max_output_tokens),
            temperature=0.1 if is_mgmt else 0.2,
            timeout_seconds=self._cfg.ai.request_timeout_seconds,
        )
        draft = result.text.strip()
        # Run a second AI pass to fact-check every number against the source
        # data before the reply is sent — prevents hallucinated figures.
        if warehouse_block:
            draft = self._validate_draft(draft, warehouse_block, rep_question=last_body)
        return draft


# ================================================================ auto-reply worker

class _AutoReplyWorker(_AiReplyWorker):
    """Generate an AI reply and send it automatically without a compose dialog.

    Extends ``_AiReplyWorker`` and re-uses ``_generate()``. After generation
    it sends via SMTP and persists the outbound message to SQLite.

    Signals:
        replied(str)           — rep name on successful send.
        send_error(str, str)   — (rep_name, error_message) on failure.
    """

    replied = Signal(str)
    send_error = Signal(str, str)

    def run(self) -> None:  # type: ignore[override]
        rep_name = self._conv.rep_name or self._conv.rep_id
        try:
            draft = self._generate()

            rep_email = self._cfg.rep_emails.get(self._conv.rep_id, "")
            # For rep-initiated conversations the rep_id may BE the email address,
            # or we can fall back to the From: of their last inbound message.
            if not rep_email and "@" in self._conv.rep_id:
                rep_email = self._conv.rep_id
            if not rep_email:
                last_inbound = next(
                    (m for m in reversed(self._messages) if m.direction == "inbound"),
                    None,
                )
                if last_inbound:
                    rep_email = _parse_email_address(last_inbound.from_address)
            if not rep_email:
                raise ValueError(f"No email address on file for {rep_name}")

            smtp_ok = bool(
                self._cfg.email.smtp_host
                and self._cfg.email.smtp_username
                and self._cfg.email.enable_outbound_send
            )
            if not smtp_ok:
                raise ValueError("SMTP not configured or outbound sending disabled")

            # Build threading headers from conversation history.
            last_inbound_id = next(
                (m.message_id for m in reversed(self._messages)
                 if m.direction == "inbound" and m.message_id),
                "",
            )
            all_msg_ids = " ".join(
                m.message_id for m in self._messages if m.message_id
            )

            client = EmailClient(self._cfg.email)
            subj = (
                self._conv.subject
                if self._conv.subject.startswith("Re:")
                else f"Re: {self._conv.subject}"
            )
            is_mgmt = self._is_management_sender()
            html_body = _ai_text_to_html(draft, is_management=is_mgmt)
            res = client.send(
                to_address=rep_email,
                subject=subj,
                body_text=draft,
                body_html=html_body,
                in_reply_to=last_inbound_id,
                references=all_msg_ids,
            )

            if not res.ok:
                raise RuntimeError(f"SMTP send failed: {res.error}")

            try:
                save_message(
                    conversation_id=self._conv.id,
                    direction="outbound",
                    message_id=res.message_id,
                    in_reply_to=last_inbound_id,
                    from_address=self._cfg.email.smtp_from_address or "(manager)",
                    to_address=rep_email,
                    subject=subj,
                    body_text=draft,
                    body_html=html_body,
                    ai_reasoning="Auto-generated and sent by AI assistant.",
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("save_message after auto-reply: %s", exc)

            self.replied.emit(rep_name)
        except Exception as exc:  # noqa: BLE001
            log.exception("Auto-reply failed for %s", rep_name)
            self.send_error.emit(rep_name, str(exc))
        finally:
            self.done.emit()


# ================================================================ compose dialog

class _ReplyComposeDialog(QDialog):
    """Polished reply compose window — AI draft editable before send."""

    sent = Signal()

    def __init__(
        self,
        cfg: AppConfig,
        conv: Conversation,
        messages: list[Message],
        draft_text: str,
        rep_email: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._conv = conv
        self._messages = messages
        self._rep_email = rep_email

        # Last inbound message-id for proper In-Reply-To threading
        self._last_msg_id = next(
            (m.message_id for m in reversed(messages) if m.direction == "inbound" and m.message_id),
            "",
        )

        self.setWindowTitle(f"Reply to {conv.rep_name or conv.rep_id}")
        self.setMinimumSize(760, 580)
        self.resize(840, 660)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(10)

        # ---- Header (To / Subject) -------------------------------------------
        hdr = QWidget()
        hdr.setStyleSheet(
            f"QWidget {{ background:{SURFACE_ALT}; border:1px solid {BORDER};"
            f" border-radius:8px; }}"
        )
        hdr_v = QVBoxLayout(hdr)
        hdr_v.setContentsMargins(12, 8, 12, 8)
        hdr_v.setSpacing(2)
        to_txt = rep_email or "<em>No email address on file — add it in Sales Reps</em>"
        subj = f"Re: {conv.subject}"
        hdr_v.addWidget(QLabel(f"<b>To:</b> &nbsp; {to_txt}"))
        hdr_v.addWidget(QLabel(f"<b>Subject:</b> &nbsp; {subj}"))
        root.addWidget(hdr)

        # ---- Collapsible thread history --------------------------------------
        self._history_visible = True
        toggle_row = QHBoxLayout()
        self._toggle_btn = QPushButton("▼  Thread history")
        self._toggle_btn.setFlat(True)
        self._toggle_btn.setStyleSheet(
            f"QPushButton {{ color:{ACCENT}; font-size:12px; font-weight:600;"
            f" text-align:left; border:none; padding:0; }}"
            f"QPushButton:hover {{ color:#1D4ED8; }}"
        )
        self._toggle_btn.clicked.connect(self._toggle_history)
        toggle_row.addWidget(self._toggle_btn)
        toggle_row.addStretch()
        root.addLayout(toggle_row)

        self.history_view = QTextBrowser()
        self.history_view.setFixedHeight(170)
        self.history_view.setStyleSheet(
            f"QTextBrowser {{ background:{SURFACE}; border:1px solid {BORDER};"
            f" border-radius:6px; padding:10px 12px; color:{TEXT}; font-size:12px; }}"
        )
        self.history_view.setHtml(_render_thread_html(messages))
        root.addWidget(self.history_view)

        # ---- Reply body label -----------------------------------------------
        reply_lbl = QLabel("Your reply:")
        reply_lbl.setStyleSheet(f"color:{TEXT_MUTED}; font-size:12px; font-weight:600;")
        root.addWidget(reply_lbl)

        # ---- Editable body --------------------------------------------------
        self.body_edit = QTextEdit()
        self.body_edit.setPlaceholderText("Draft your reply here…")
        self.body_edit.setPlainText(draft_text)
        self.body_edit.setStyleSheet(
            f"QTextEdit {{ background:{SURFACE}; border:1px solid {BORDER};"
            f" border-radius:6px; padding:10px 12px; color:{TEXT}; font-size:13px; }}"
            f"QTextEdit:focus {{ border-color:{ACCENT}; }}"
        )
        self.body_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(self.body_edit, 1)

        # ---- Status + buttons -----------------------------------------------
        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")
        root.addWidget(self.status_lbl)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedHeight(36)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        btn_row.addStretch()

        self.send_btn = QPushButton("✉  Send Reply")
        self.send_btn.setFixedHeight(36)
        self.send_btn.setStyleSheet(
            f"QPushButton {{ background:{ACCENT}; color:#fff; border:none;"
            f" border-radius:6px; font-weight:600; padding:0 20px; }}"
            f"QPushButton:hover {{ background:#1D4ED8; }}"
            f"QPushButton:pressed {{ background:#1E40AF; }}"
            f"QPushButton:disabled {{ background:#93C5FD; color:#fff; }}"
        )
        smtp_ok = bool(
            cfg.email.smtp_host
            and cfg.email.smtp_username
            and cfg.email.enable_outbound_send
        )
        if not smtp_ok:
            self.send_btn.setEnabled(False)
            self.send_btn.setToolTip("Outbound SMTP is not configured or disabled. Enable in Settings → Email.")
        self.send_btn.clicked.connect(self._send)
        btn_row.addWidget(self.send_btn)
        root.addLayout(btn_row)

    def _toggle_history(self) -> None:
        self._history_visible = not self._history_visible
        self.history_view.setVisible(self._history_visible)
        self._toggle_btn.setText(f"{'▼' if self._history_visible else '▶'}  Thread history")

    def _send(self) -> None:
        body = self.body_edit.toPlainText().strip()
        if not body:
            self.status_lbl.setText("⚠  Reply body is empty.")
            return
        if not self._rep_email:
            self.status_lbl.setText("⚠  No email address on file. Add it in Sales Reps.")
            return

        self.send_btn.setEnabled(False)
        self.status_lbl.setText("Sending…")

        subj = f"Re: {self._conv.subject}"
        outbound_ids = [m.message_id for m in self._messages if m.direction == "outbound" and m.message_id]
        references = " ".join(outbound_ids)

        html_body = _ai_text_to_html(body)
        client = EmailClient(self._cfg.email)
        res = client.send(
            to_address=self._rep_email,
            subject=subj,
            body_text=body,
            body_html=html_body,
            in_reply_to=self._last_msg_id,
            references=references,
        )

        if not res.ok:
            self.status_lbl.setText(f"⚠  Send failed: {res.error}")
            self.send_btn.setEnabled(True)
            return

        try:
            save_message(
                conversation_id=self._conv.id,
                direction="outbound",
                message_id=res.message_id,
                in_reply_to=self._last_msg_id,
                from_address=self._cfg.email.smtp_from_address or "(manager)",
                to_address=self._rep_email,
                subject=subj,
                body_text=body,
                body_html=html_body,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("save_message error after reply: %s", exc)

        self.sent.emit()
        self.accept()


# ================================================================ AI reply HTML formatter

def _ai_text_to_html(raw: str, is_management: bool = False) -> str:  # noqa: C901
    """Convert an AI plain-text reply (with optional ASCII tables) to a
    styled HTML email matching the app's leaderboard aesthetic.

    Tables are auto-detected by separator lines (runs of ``-`` / ``─``
    chars separated by spaces). Currency / percentage cells are right-aligned;
    name / description cells are left-aligned. A clean company header and
    footer are wrapped around the body.
    """
    import html as _esc

    # ── design tokens (matches leaderboard palette) ────────────────────────────
    C_BG      = "#F8FAFC"
    C_CARD    = "#FFFFFF"
    C_HDR     = "#0F172A"
    C_HDR_TXT = "#F1F5F9"
    C_BORDER  = "#E2E8F0"
    C_STRIPE  = "#F8FAFC"
    C_TOTAL   = "#1E293B"
    C_TTXT    = "#F1F5F9"
    C_TEXT    = "#1E293B"
    C_MUTED   = "#64748B"

    _SEP_RE = re.compile(r'^[-─═╌ \u2500\u2501\u2550]{5,}$')
    _NUM_RE = re.compile(r'^[\$\-+]?[\d,]+(\.\d+)?%?$')

    def _is_sep(line: str) -> bool:
        s = line.strip()
        return bool(s) and bool(_SEP_RE.match(s)) and len(s) >= 5

    def _is_numeric(s: str) -> bool:
        return bool(_NUM_RE.match(s.strip()))

    def _split_cols(line: str) -> list[str]:
        # Split on 2+ consecutive spaces; preserve single words
        return [c.strip() for c in re.split(r' {2,}', line.rstrip()) if c.strip()]

    def _th(content: str, is_num: bool) -> str:
        align = "right" if is_num else "left"
        return (
            f'<th style="padding:8px 14px;text-align:{align};color:{C_HDR_TXT};'
            f'background:{C_HDR};font-weight:600;white-space:nowrap;'
            f'font-size:12px;letter-spacing:.3px;">'
            f'{_esc.escape(content)}</th>'
        )

    def _td(content: str, is_num: bool, bg: str, clr: str, bold: bool = False) -> str:
        align = "right" if is_num else "left"
        weight = "600" if bold else "400"
        return (
            f'<td style="padding:6px 14px;text-align:{align};color:{clr};'
            f'background:{bg};font-weight:{weight};'
            f'font-variant-numeric:tabular-nums;'
            f'border-top:1px solid {C_BORDER};white-space:nowrap;">'
            f'{_esc.escape(content)}</td>'
        )

    def _render_table_block(block: list[str]) -> str:
        sep_idx = next((i for i, ln in enumerate(block) if _is_sep(ln)), None)
        if sep_idx is None:
            return _render_text_block(block)

        header_lines = [ln for ln in block[:sep_idx] if ln.strip()]
        data_lines   = [ln for ln in block[sep_idx + 1:] if ln.strip() and not _is_sep(ln)]
        if not header_lines:
            return _render_text_block(block)

        header_cells = _split_cols(header_lines[-1])
        n_cols = max(len(header_cells), 1)

        # Infer numeric columns from the first few data rows
        num_col: list[bool] = [False] * n_cols
        for dl in data_lines[:6]:
            for ci, c in enumerate(_split_cols(dl)[:n_cols]):
                if _is_numeric(c):
                    num_col[ci] = True

        rows: list[str] = []

        # Header row(s)
        for hl in header_lines:
            hcells = _split_cols(hl)
            while len(hcells) < n_cols:
                hcells.append("")
            rows.append(
                "<tr>"
                + "".join(_th(c, num_col[i] if i < len(num_col) else False)
                          for i, c in enumerate(hcells[:n_cols]))
                + "</tr>"
            )

        # Data rows
        for ri, dl in enumerate(data_lines):
            cells = _split_cols(dl)
            while len(cells) < n_cols:
                cells.append("")
            is_total = bool(cells and re.match(r'^total', cells[0], re.I))
            bg   = C_TOTAL if is_total else (C_STRIPE if ri % 2 else C_CARD)
            clr  = C_TTXT  if is_total else C_TEXT
            rows.append(
                "<tr>"
                + "".join(_td(c, num_col[i] if i < len(num_col) else False, bg, clr, is_total)
                          for i, c in enumerate(cells[:n_cols]))
                + "</tr>"
            )

        return (
            f'<table style="border-collapse:collapse;width:100%;margin:14px 0;'
            f'font-size:13px;border:1px solid {C_BORDER};border-radius:4px;overflow:hidden;">'
            + "".join(rows)
            + "</table>"
        )

    def _render_text_block(block: list[str]) -> str:
        parts: list[str] = []
        for line in block:
            s = line.strip()
            if not s:
                continue
            # Section heading: ALL CAPS line or ends with ':'
            is_heading = (
                s.endswith(":")
                or (s == s.upper() and len(s) > 4 and len(s) < 90
                    and not re.search(r'\d{4}', s))
            )
            if is_heading:
                parts.append(
                    f'<p style="margin:18px 0 3px;font-size:11px;font-weight:700;'
                    f'letter-spacing:.5px;text-transform:uppercase;color:{C_MUTED};">'
                    f'{_esc.escape(s)}</p>'
                )
            else:
                parts.append(
                    f'<p style="margin:5px 0;font-size:14px;line-height:1.6;color:{C_TEXT};">'
                    f'{_esc.escape(s)}</p>'
                )
        return "\n".join(parts)

    # ── split raw text into blocks (blank-line delimited) ─────────────────────
    lines = raw.split("\n")
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if not line.strip():
            if current:
                blocks.append(current)
                current = []
        else:
            current.append(line)
    if current:
        blocks.append(current)

    # ── render each block ──────────────────────────────────────────────────────
    body_parts: list[str] = []
    for block in blocks:
        if any(_is_sep(ln) for ln in block):
            body_parts.append(_render_table_block(block))
        else:
            body_parts.append(_render_text_block(block))

    body_html = "\n".join(body_parts)

    label = "Full Company Data" if is_management else "Territory Data"
    return (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8"></head>'
        f'<body style="margin:0;padding:0;background:{C_BG};'
        f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI Variable\',\'Segoe UI\',sans-serif;">'
        f'<div style="max-width:680px;margin:24px auto;">'
        f'<div style="background:{C_CARD};border:1px solid {C_BORDER};border-radius:8px;'
        f'overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.06);">'
        f'<div style="background:{C_HDR};padding:12px 20px;display:flex;align-items:center;gap:12px;">'
        f'<span style="color:{C_HDR_TXT};font-size:13px;font-weight:600;letter-spacing:.3px;">'
        f'Sales Data Reply</span>'
        f'<span style="color:#64748B;font-size:11px;margin-left:auto;">{label}</span>'
        f'</div>'
        f'<div style="padding:20px 24px 16px;">'
        f'{body_html}'
        f'</div>'
        f'<div style="padding:10px 24px 14px;border-top:1px solid {C_BORDER};background:{C_BG};">'
        f'<span style="font-size:11px;color:{C_MUTED};">'
        f'Generated from live warehouse data · Reply with any follow-up question.'
        f'</span></div></div></div></body></html>'
    )


# ================================================================ shared renderer

def _render_thread_html(messages: list[Message]) -> str:
    """Shared thread renderer used by both tabs and the compose dialog."""
    if not messages:
        return f"<p style='color:{TEXT_MUTED}'>No messages yet.</p>"
    parts = []
    for msg in messages:
        is_in = msg.direction == "inbound"
        bg = "#EFF6FF" if is_in else "#F0FDF4"
        border = "#BFDBFE" if is_in else "#BBF7D0"
        who = f"From: {msg.from_address}" if is_in else f"To: {msg.to_address}"
        header = (
            f"<div style='font-size:11px;color:{TEXT_MUTED};margin-bottom:6px;'>"
            f"{'← Rep reply' if is_in else '→ Sent'} · {msg.sent_at[:16]} · {who}"
            f"</div>"
        )
        if msg.body_html:
            body_content = msg.body_html
        else:
            esc = (msg.body_text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            body_content = f"<div style='white-space:pre-wrap;font-size:13px;'>{esc[:4000]}</div>"
        parts.append(
            f"<div style='background:{bg};border-radius:8px;padding:10px 14px;"
            f"margin:8px 0;border:1px solid {border};'>"
            + header + body_content + "</div>"
        )
    return "".join(parts)


# ================================================================ main view

class ConversationsView(QWidget):
    """Full conversation management view with AI-powered reply drafting."""

    needs_review_changed = Signal(int)

    def __init__(
        self,
        cfg: AppConfig,
        get_db: Callable | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._get_db = get_db
        self._conversations: list[Conversation] = []
        self._action_items: list[ActionItem] = []
        self._poll_workers: list[_ImapPollWorker] = []
        self._reply_workers: list[_AiReplyWorker] = []
        self._auto_workers: list[_AutoReplyWorker] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)

        root.addWidget(
            ViewHeader(
                "Conversations",
                "Every email thread the assistant has opened with a rep — "
                "plus their replies, extracted commitments, and pending approvals.",
            )
        )

        # ---- IMAP poll bar -----------------------------------------------
        poll_row = QHBoxLayout()
        poll_row.setSpacing(8)
        self.poll_btn = QPushButton("🔄  Check for new replies")
        self.poll_btn.setFixedHeight(30)
        self.poll_btn.clicked.connect(self._poll_imap)
        poll_row.addWidget(self.poll_btn)
        self.poll_status = QLabel("")
        self.poll_status.setStyleSheet(f"color:{TEXT_MUTED};font-size:12px;")
        poll_row.addWidget(self.poll_status, 1)
        root.addLayout(poll_row)

        imap_ok = bool(cfg.email.imap_host and cfg.email.imap_username)
        smtp_ok = bool(cfg.email.smtp_host and cfg.email.smtp_username and cfg.email.enable_outbound_send)
        ai_configured = bool(
            getattr(cfg.ai, "provider", None) and getattr(cfg.ai, "api_username", None)
        )
        self._auto_reply_enabled = imap_ok and smtp_ok and ai_configured

        if not imap_ok:
            self.poll_btn.setEnabled(False)
            self.poll_btn.setToolTip("Configure IMAP in Settings → Email to enable automatic reply detection.")
            self.poll_status.setText("IMAP not configured — configure in Settings → Email.")
        elif self._auto_reply_enabled:
            wl_count = len(self._cfg.email.auto_reply_whitelist or [])
            mgmt_count = len(self._cfg.email.auto_reply_management_emails or [])
            total_count = wl_count + mgmt_count
            if total_count:
                parts = []
                if wl_count:
                    parts.append(f"{wl_count} rep(s)")
                if mgmt_count:
                    parts.append(f"{mgmt_count} management")
                self.poll_status.setText(
                    f"Auto-reply active for {', '.join(parts)} — checking every 2 min."
                )
            else:
                self.poll_status.setText(
                    "Auto-reply paused — add rep email addresses in "
                    "Email Settings → Auto-Reply tab to activate."
                )
        else:
            missing = []
            if not smtp_ok:
                missing.append("SMTP")
            if not ai_configured:
                missing.append("AI provider")
            self.poll_status.setText(
                f"Auto-reply disabled (configure {', '.join(missing)} to enable)."
            )

        # Background auto-poll timer (every 2 minutes when fully configured).
        self._auto_poll_timer = QTimer(self)
        self._auto_poll_timer.timeout.connect(self._auto_poll_cycle)
        if self._auto_reply_enabled:
            self._auto_poll_timer.start(2 * 60 * 1000)  # 2 minutes

        # ---- Tabs --------------------------------------------------------
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._build_all_tab(), "All Conversations")
        self.tabs.addTab(self._build_review_tab(), "Needs Review")
        self.tabs.addTab(self._build_actions_tab(), "Action Items")
        self.tabs.addTab(self._build_facts_tab(), "Rep Facts")
        root.addWidget(self.tabs, 1)

        QTimer.singleShot(0, self.refresh)

    # ================================================================ tab builders

    def _build_all_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(6)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(4)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)
        self.filter_all = QPushButton("All")
        self.filter_active = QPushButton("Active")
        self.filter_review = QPushButton("Needs reply")
        for btn in (self.filter_all, self.filter_active, self.filter_review):
            btn.setCheckable(True)
            btn.setFixedHeight(28)
            filter_row.addWidget(btn)
        filter_row.addStretch(1)
        self.filter_all.setChecked(True)
        self.filter_all.clicked.connect(lambda: self._apply_filter(None))
        self.filter_active.clicked.connect(lambda: self._apply_filter("active"))
        self.filter_review.clicked.connect(lambda: self._apply_filter("review"))
        lv.addLayout(filter_row)

        self.conv_list = QListWidget()
        self.conv_list.setAlternatingRowColors(True)
        self.conv_list.setMinimumWidth(280)
        self.conv_list.itemSelectionChanged.connect(self._on_conv_selected)
        lv.addWidget(self.conv_list, 1)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        lv.addWidget(refresh_btn)
        splitter.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(6)

        self.thread_label = QLabel("")
        self.thread_label.setStyleSheet("font-weight:600; font-size:13px;")
        rv.addWidget(self.thread_label)

        self.thread_view = QTextBrowser()
        self.thread_view.setOpenExternalLinks(False)
        self.thread_view.setStyleSheet(
            f"QTextBrowser {{ background:{SURFACE}; border:1px solid {BORDER};"
            f" border-radius:8px; padding:14px; color:{TEXT}; }}"
        )
        rv.addWidget(self.thread_view, 1)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 700])
        layout.addWidget(splitter, 1)
        return w

    def _build_review_tab(self) -> QWidget:
        """Tab 1: Needs Review — full thread view + AI Reply / Manual Reply actions."""
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(6)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: list of conversations needing a reply
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)

        self.review_list = QListWidget()
        self.review_list.setAlternatingRowColors(True)
        self.review_list.setMinimumWidth(270)
        self.review_list.itemSelectionChanged.connect(self._on_review_selected)
        lv.addWidget(self.review_list, 1)
        splitter.addWidget(left)

        # Right: full thread + action buttons
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(8)

        self.review_thread_label = QLabel("")
        self.review_thread_label.setStyleSheet("font-weight:600; font-size:13px;")
        rv.addWidget(self.review_thread_label)

        self.review_thread = QTextBrowser()
        self.review_thread.setOpenExternalLinks(False)
        self.review_thread.setStyleSheet(
            f"QTextBrowser {{ background:{SURFACE}; border:1px solid {BORDER};"
            f" border-radius:8px; padding:14px; color:{TEXT}; }}"
        )
        rv.addWidget(self.review_thread, 1)

        # Action row
        action_row = QHBoxLayout()
        action_row.setSpacing(8)

        self.ai_reply_btn = QPushButton("✨  Draft AI Reply")
        self.ai_reply_btn.setFixedHeight(36)
        self.ai_reply_btn.setEnabled(False)
        self.ai_reply_btn.setStyleSheet(
            f"QPushButton {{ background:{ACCENT}; color:#fff; border:none;"
            f" border-radius:6px; font-weight:600; padding:0 16px; }}"
            f"QPushButton:hover {{ background:#1D4ED8; }}"
            f"QPushButton:pressed {{ background:#1E40AF; }}"
            f"QPushButton:disabled {{ background:#93C5FD; color:#fff; }}"
        )
        ai_ok = bool(
            getattr(self._cfg.ai, "provider", None)
            and getattr(self._cfg.ai, "api_username", None)
        )
        if not ai_ok:
            self.ai_reply_btn.setToolTip("Configure an AI provider in Settings → AI to enable reply drafting.")
        self.ai_reply_btn.clicked.connect(self._draft_ai_reply)
        action_row.addWidget(self.ai_reply_btn)

        self.manual_reply_btn = QPushButton("Mark as replied (manual)")
        self.manual_reply_btn.setFixedHeight(36)
        self.manual_reply_btn.setEnabled(False)
        self.manual_reply_btn.setToolTip(
            "Records that you replied outside the app, removing this from the queue."
        )
        self.manual_reply_btn.clicked.connect(self._mark_replied)
        action_row.addWidget(self.manual_reply_btn)

        action_row.addStretch(1)

        self.review_status = QLabel("")
        self.review_status.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")
        action_row.addWidget(self.review_status)
        rv.addLayout(action_row)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([270, 730])
        layout.addWidget(splitter, 1)
        return w

    def _build_actions_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("Open commitments extracted from rep replies:"))
        hdr.addStretch(1)
        self.show_done_btn = QPushButton("Show done")
        self.show_done_btn.setCheckable(True)
        self.show_done_btn.clicked.connect(self._refresh_action_list)
        hdr.addWidget(self.show_done_btn)
        layout.addLayout(hdr)

        self.action_list = QListWidget()
        self.action_list.setAlternatingRowColors(True)
        layout.addWidget(self.action_list, 1)

        btn_row = QHBoxLayout()
        self.mark_done_btn = QPushButton("Mark done")
        self.mark_done_btn.setEnabled(False)
        self.mark_done_btn.clicked.connect(self._mark_action_done)
        self.mark_skip_btn = QPushButton("Skip")
        self.mark_skip_btn.setEnabled(False)
        self.mark_skip_btn.clicked.connect(self._mark_action_skipped)
        btn_row.addWidget(self.mark_done_btn)
        btn_row.addWidget(self.mark_skip_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)
        self.action_list.itemSelectionChanged.connect(self._on_action_selected)
        return w

    def _build_facts_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        hdr = QHBoxLayout()
        intro = QLabel(
            "Durable facts the assistant remembers from rep replies "
            "(e.g. an account is closed). These are honored in future emails "
            "and replies. Deactivate or delete any that are wrong."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{TEXT_MUTED};font-size:12px;")
        hdr.addWidget(intro, 1)
        self.facts_show_inactive = QPushButton("Show inactive")
        self.facts_show_inactive.setCheckable(True)
        self.facts_show_inactive.setFixedHeight(28)
        self.facts_show_inactive.clicked.connect(self._refresh_facts_table)
        hdr.addWidget(self.facts_show_inactive)
        layout.addLayout(hdr)

        self.facts_table = QTableWidget(0, 6)
        self.facts_table.setHorizontalHeaderLabels(
            ["Rep", "Account", "Fact", "Type", "Source", "When"]
        )
        self.facts_table.verticalHeader().setVisible(False)
        self.facts_table.setAlternatingRowColors(True)
        self.facts_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.facts_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.facts_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hh = self.facts_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.facts_table.itemSelectionChanged.connect(self._on_fact_selected)
        layout.addWidget(self.facts_table, 1)

        btn_row = QHBoxLayout()
        self.fact_toggle_btn = QPushButton("Deactivate")
        self.fact_toggle_btn.setEnabled(False)
        self.fact_toggle_btn.clicked.connect(self._toggle_fact_active)
        self.fact_delete_btn = QPushButton("Delete")
        self.fact_delete_btn.setEnabled(False)
        self.fact_delete_btn.clicked.connect(self._delete_fact)
        btn_row.addWidget(self.fact_toggle_btn)
        btn_row.addWidget(self.fact_delete_btn)
        btn_row.addStretch(1)
        self.facts_status = QLabel("")
        self.facts_status.setStyleSheet(f"color:{TEXT_MUTED};font-size:12px;")
        btn_row.addWidget(self.facts_status)
        layout.addLayout(btn_row)
        return w

    def _refresh_facts_table(self) -> None:
        active_only = not self.facts_show_inactive.isChecked()
        try:
            facts = list_rep_facts(active_only=active_only)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to load rep facts: %s", exc)
            facts = []
        self._facts = facts
        tbl = self.facts_table
        tbl.setRowCount(0)
        for f in facts:
            row = tbl.rowCount()
            tbl.insertRow(row)
            rep = (f.rep_name or f.rep_id or "—").strip()
            acct = f.account_label or f.account_number or "—"
            when = (f.created_at or "")[:16]
            cells = [
                rep,
                acct,
                f.fact_text,
                f.fact_type.replace("_", " "),
                f.source.replace("_", " "),
                when,
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, f.id)
                if not f.active:
                    item.setForeground(Qt.GlobalColor.gray)
                tbl.setItem(row, col, item)
        n = len(facts)
        self.facts_status.setText(
            f"{n} fact{'s' if n != 1 else ''}" + ("" if active_only else " (incl. inactive)")
        )
        self.fact_toggle_btn.setEnabled(False)
        self.fact_delete_btn.setEnabled(False)

    def _selected_fact(self):
        rows = self.facts_table.selectionModel().selectedRows() if self.facts_table.selectionModel() else []
        if not rows:
            return None
        row = rows[0].row()
        item = self.facts_table.item(row, 0)
        if item is None:
            return None
        fid = item.data(Qt.ItemDataRole.UserRole)
        for f in getattr(self, "_facts", []):
            if f.id == fid:
                return f
        return None

    def _on_fact_selected(self) -> None:
        f = self._selected_fact()
        self.fact_toggle_btn.setEnabled(f is not None)
        self.fact_delete_btn.setEnabled(f is not None)
        if f is not None:
            self.fact_toggle_btn.setText("Reactivate" if not f.active else "Deactivate")

    def _toggle_fact_active(self) -> None:
        f = self._selected_fact()
        if f is None:
            return
        try:
            set_rep_fact_active(f.id, not f.active)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to toggle fact: %s", exc)
        self._refresh_facts_table()

    def _delete_fact(self) -> None:
        f = self._selected_fact()
        if f is None:
            return
        try:
            delete_rep_fact(f.id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to delete fact: %s", exc)
        self._refresh_facts_table()

    # ================================================================ data

    def refresh(self) -> None:
        try:
            self._conversations = list_conversations()
        except Exception as exc:
            log.warning("Failed to load conversations: %s", exc)
            self._conversations = []
        try:
            self._action_items = list_action_items(status="open")
        except Exception as exc:
            log.warning("Failed to load action items: %s", exc)
            self._action_items = []
        self._populate_conv_list(self._conversations)
        self._populate_review_list()
        self._refresh_action_list()
        self._refresh_facts_table()
        self._update_tab_badges()

    def _update_tab_badges(self) -> None:
        needs = sum(1 for c in self._conversations if c.needs_reply)
        open_actions = len(self._action_items)
        self.tabs.setTabText(1, f"Needs Review {'●' if needs else ''}")
        self.tabs.setTabText(2, f"Action Items ({open_actions})" if open_actions else "Action Items")
        self.needs_review_changed.emit(needs)
        self.tabs.tabBar().setTabTextColor(1, QColor(DANGER) if needs > 0 else QColor())

    # ================================================================ all conversations tab

    def _apply_filter(self, mode: str | None) -> None:
        self.filter_all.setChecked(mode is None)
        self.filter_active.setChecked(mode == "active")
        self.filter_review.setChecked(mode == "review")
        if mode is None:
            convs = self._conversations
        elif mode == "active":
            convs = [c for c in self._conversations if c.status == "active"]
        else:
            convs = [c for c in self._conversations if c.needs_reply]
        self._populate_conv_list(convs)

    def _populate_conv_list(self, convs: list[Conversation]) -> None:
        self.conv_list.clear()
        if not convs:
            item = QListWidgetItem(
                "No conversations yet.\n\n"
                "Send your first weekly email from the Weekly Email tab\n"
                "to start tracking threads here."
            )
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            item.setForeground(Qt.GlobalColor.gray)
            self.conv_list.addItem(item)
            return
        for c in convs:
            badge = "  🔴 REPLY NEEDED" if c.needs_reply else ""
            rep = c.rep_name or c.rep_id
            item = QListWidgetItem(
                f"{rep}{badge}\n"
                f"  {c.subject[:60]}\n"
                f"  {c.last_activity_at[:16]}"
            )
            item.setData(Qt.ItemDataRole.UserRole, c.id)
            if c.needs_reply:
                item.setForeground(Qt.GlobalColor.red)
            self.conv_list.addItem(item)

    def _on_conv_selected(self) -> None:
        items = self.conv_list.selectedItems()
        if not items:
            return
        conv_id = items[0].data(Qt.ItemDataRole.UserRole)
        if not isinstance(conv_id, int):
            return
        conv = next((c for c in self._conversations if c.id == conv_id), None)
        if conv is None:
            return
        rep = conv.rep_name or conv.rep_id
        self.thread_label.setText(f"{rep} — {conv.subject}  [{conv.status}]")
        try:
            messages = list_messages(conv.id)
        except Exception:
            messages = []
        if messages:
            self.thread_view.setHtml(_render_thread_html(messages))
        else:
            self.thread_view.setHtml(
                f"<p style='color:{TEXT_MUTED}'>No messages recorded in this thread yet.</p>"
                "<p style='color:#64748B;font-size:12px;'>Messages appear once emails are sent "
                "via the Weekly Email tab and reps reply.</p>"
            )

    # ================================================================ needs review tab

    def _populate_review_list(self) -> None:
        self.review_list.clear()
        needs = [c for c in self._conversations if c.needs_reply]
        if not needs:
            item = QListWidgetItem("✓  All caught up — no unanswered replies.")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            item.setForeground(Qt.GlobalColor.gray)
            self.review_list.addItem(item)
            self._set_review_btns(False)
            return
        for c in needs:
            rep = c.rep_name or c.rep_id
            last = c.last_inbound_at[:16] if c.last_inbound_at else "?"
            item = QListWidgetItem(f"🔴  {rep}\n     {c.subject[:55]}\n     Last reply: {last}")
            item.setData(Qt.ItemDataRole.UserRole, c.id)
            self.review_list.addItem(item)

    def _on_review_selected(self) -> None:
        items = self.review_list.selectedItems()
        if not items:
            self._set_review_btns(False)
            self.review_thread.clear()
            return
        conv_id = items[0].data(Qt.ItemDataRole.UserRole)
        if not isinstance(conv_id, int):
            self._set_review_btns(False)
            return
        conv = next((c for c in self._conversations if c.id == conv_id), None)
        if conv is None:
            return
        rep = conv.rep_name or conv.rep_id
        self.review_thread_label.setText(f"{rep} — {conv.subject}")
        try:
            messages = list_messages(conv.id)
        except Exception:
            messages = []
        self.review_thread.setHtml(_render_thread_html(messages))
        # Scroll to bottom so latest reply is immediately visible
        sb = self.review_thread.verticalScrollBar()
        sb.setValue(sb.maximum())
        ai_ok = bool(getattr(self._cfg.ai, "provider", None) and getattr(self._cfg.ai, "api_username", None))
        self.ai_reply_btn.setEnabled(ai_ok)
        self.manual_reply_btn.setEnabled(True)
        self.review_status.setText("")

    def _set_review_btns(self, enabled: bool) -> None:
        self.ai_reply_btn.setEnabled(False)  # Only enabled when item selected + AI configured
        self.manual_reply_btn.setEnabled(enabled)

    # ---------------------------------------------------------------- AI reply

    def _draft_ai_reply(self) -> None:
        items = self.review_list.selectedItems()
        if not items:
            return
        conv_id = items[0].data(Qt.ItemDataRole.UserRole)
        if not isinstance(conv_id, int):
            return
        conv = next((c for c in self._conversations if c.id == conv_id), None)
        if conv is None:
            return
        try:
            messages = list_messages(conv.id)
        except Exception:
            messages = []

        self.ai_reply_btn.setEnabled(False)
        self.manual_reply_btn.setEnabled(False)
        self.review_status.setText("✨  Drafting reply…")

        worker = _AiReplyWorker(self._cfg, conv, messages, self._get_db, parent=self)
        self._reply_workers.append(worker)
        worker.draft_ready.connect(lambda draft: self._on_draft_ready(draft, conv, messages))
        worker.error.connect(self._on_draft_error)
        worker.done.connect(lambda: self._on_reply_worker_done(worker))
        worker.start()

    def _on_draft_ready(self, draft: str, conv: Conversation, messages: list[Message]) -> None:
        self.review_status.setText("")
        rep_email = self._cfg.rep_emails.get(conv.rep_id, "")
        dlg = _ReplyComposeDialog(self._cfg, conv, messages, draft, rep_email, parent=self)
        dlg.sent.connect(self.refresh)
        dlg.exec()

    def _on_draft_error(self, msg: str) -> None:
        self.review_status.setText(f"⚠  AI error: {msg[:120]}")

    def _on_reply_worker_done(self, worker: _AiReplyWorker) -> None:
        items = self.review_list.selectedItems()
        has_sel = bool(items and isinstance(items[0].data(Qt.ItemDataRole.UserRole), int))
        ai_ok = bool(getattr(self._cfg.ai, "provider", None) and getattr(self._cfg.ai, "api_username", None))
        self.ai_reply_btn.setEnabled(has_sel and ai_ok)
        self.manual_reply_btn.setEnabled(has_sel)
        try:
            self._reply_workers.remove(worker)
        except ValueError:
            pass

    # ---------------------------------------------------------------- manual mark replied

    def _mark_replied(self) -> None:
        items = self.review_list.selectedItems()
        if not items:
            return
        conv_id = items[0].data(Qt.ItemDataRole.UserRole)
        if not isinstance(conv_id, int):
            return
        try:
            conv = next((c for c in self._conversations if c.id == conv_id), None)
            if conv:
                rep_email = self._cfg.rep_emails.get(conv.rep_id, "") or conv.rep_id
                save_message(
                    conversation_id=conv_id,
                    direction="outbound",
                    from_address=self._cfg.email.smtp_from_address or "(manager)",
                    to_address=rep_email,
                    subject=conv.subject,
                    body_text="[Reply sent manually outside the app]",
                    ai_reasoning="Manual acknowledgment logged by manager.",
                )
            self.review_status.setText("Marked as replied.")
        except Exception as exc:  # noqa: BLE001
            self.review_status.setText(f"Error: {exc}")
        self.refresh()

    # ================================================================ action items tab

    def _refresh_action_list(self) -> None:
        self.action_list.clear()
        show_done = self.show_done_btn.isChecked()
        try:
            items = list_action_items(status=None if show_done else "open")
        except Exception:
            items = []
        if not items:
            placeholder = QListWidgetItem(
                "No open action items.\n\nAction items are extracted from rep replies "
                "(e.g. 'I'll call them Friday')."
            )
            placeholder.setFlags(placeholder.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            placeholder.setForeground(Qt.GlobalColor.gray)
            self.action_list.addItem(placeholder)
            self.mark_done_btn.setEnabled(False)
            self.mark_skip_btn.setEnabled(False)
            return
        for ai in items:
            due = f"  Due: {ai.due_at}" if ai.due_at else ""
            icon = {"open": "○", "done": "✓", "skipped": "—"}.get(ai.status, "?")
            item = QListWidgetItem(f"{icon}  [{ai.rep_id}] {ai.description[:80]}{due}")
            item.setData(Qt.ItemDataRole.UserRole, ai.id)
            if ai.status != "open":
                item.setForeground(Qt.GlobalColor.gray)
            self.action_list.addItem(item)

    def _on_action_selected(self) -> None:
        has = bool(
            self.action_list.selectedItems()
            and isinstance(self.action_list.selectedItems()[0].data(Qt.ItemDataRole.UserRole), int)
        )
        self.mark_done_btn.setEnabled(has)
        self.mark_skip_btn.setEnabled(has)

    def _mark_action_done(self) -> None:
        self._resolve_selected_action("done")

    def _mark_action_skipped(self) -> None:
        self._resolve_selected_action("skipped")

    def _resolve_selected_action(self, new_status: str) -> None:
        items = self.action_list.selectedItems()
        if not items:
            return
        item_id = items[0].data(Qt.ItemDataRole.UserRole)
        if not isinstance(item_id, int):
            return
        try:
            resolve_action_item(item_id, new_status)
        except Exception as exc:
            log.warning("Failed to resolve action item %s: %s", item_id, exc)
        self._action_items = list_action_items(status="open")
        self._refresh_action_list()
        self._update_tab_badges()

    # ================================================================ IMAP polling

    def _poll_imap(self) -> None:
        """Manual poll triggered by the button."""
        self._start_poll(auto=False)

    def _auto_poll_cycle(self) -> None:
        """Background timer — skip if a poll is already running."""
        if self._poll_workers:
            return
        self._start_poll(auto=True)

    def _start_poll(self, auto: bool = False) -> None:
        self.poll_btn.setEnabled(False)
        if not auto:
            self.poll_status.setText("Checking inbox…")
        worker = _ImapPollWorker(self._cfg, parent=self)
        self._poll_workers.append(worker)
        worker.found.connect(lambda n: self._on_poll_found(n, auto=auto))
        worker.new_conv_ids.connect(self._on_new_conv_ids)
        worker.error.connect(self._on_poll_error)
        worker.done.connect(lambda: self._on_poll_done(worker))
        worker.start()

    def _on_poll_found(self, count: int, *, auto: bool = False) -> None:
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M")
        if count:
            suffix = "repl" + ("y" if count == 1 else "ies")
            if self._auto_reply_enabled:
                self.poll_status.setText(
                    f"✓  {count} new {suffix} — auto-replying… (last checked {ts})"
                )
            else:
                self.poll_status.setText(f"✓  {count} new {suffix} saved. Last checked: {ts}")
            self.refresh()
        elif not auto:
            self.poll_status.setText(f"✓  No new replies. Last checked: {ts}")
        else:
            # Quiet update: just refresh the timestamp
            cur = self.poll_status.text()
            if "Auto-reply active" in cur or "last checked" in cur.lower():
                self.poll_status.setText(f"Auto-reply active — last checked {ts}")

    def _on_new_conv_ids(self, conv_ids: object) -> None:
        """Trigger auto-reply for every conversation that just received a new inbound message."""
        if not self._auto_reply_enabled:
            return
        ids = list(conv_ids) if conv_ids else []
        # Refresh conversation list so we have up-to-date Conversation objects.
        try:
            all_convs = list_conversations()
        except Exception:
            return
        for conv_id in ids:
            conv = next((c for c in all_convs if c.id == conv_id), None)
            if conv is None:
                continue
            # Only auto-reply if the conversation needs a reply (latest message is inbound).
            if not conv.needs_reply:
                continue
            try:
                messages = list_messages(conv_id)
            except Exception:
                continue
            if not messages:
                continue
            rep_email = self._cfg.rep_emails.get(conv.rep_id, "")
            # For rep-initiated conversations the rep_id may BE the email address.
            if not rep_email and "@" in conv.rep_id:
                rep_email = conv.rep_id
            if not rep_email:
                log.info("Auto-reply skipped for %s — no email on file", conv.rep_id)
                continue
            # ── Whitelist gate ───────────────────────────────────────────
            # Only addresses explicitly whitelisted (rep or management) receive auto-replies.
            # An empty whitelist AND empty management list means auto-reply is inactive.
            whitelist = {
                e.strip().lower()
                for e in (self._cfg.email.auto_reply_whitelist or [])
                if e.strip()
            }
            management_set = {
                e.strip().lower()
                for e in (self._cfg.email.auto_reply_management_emails or [])
                if e.strip()
            }
            all_eligible = whitelist | management_set
            if not all_eligible:
                log.info(
                    "Auto-reply skipped for %s — whitelist is empty (add addresses "
                    "in Email Settings → Auto-Reply tab)",
                    rep_email,
                )
                continue
            if rep_email.lower() not in all_eligible:
                log.info(
                    "Auto-reply skipped for %s — address not in whitelist", rep_email
                )
                continue
            log.info("Auto-replying to conv %s (%s)", conv_id, conv.rep_name)
            self._fire_auto_reply(conv, messages)

    def _fire_auto_reply(self, conv: Conversation, messages: list[Message]) -> None:
        worker = _AutoReplyWorker(self._cfg, conv, messages, self._get_db, parent=self)
        self._auto_workers.append(worker)
        worker.replied.connect(self._on_auto_replied)
        worker.send_error.connect(self._on_auto_error)
        worker.done.connect(lambda: self._on_auto_done(worker))
        worker.start()

    def _on_auto_replied(self, rep_name: str) -> None:
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M")
        self.poll_status.setText(f"✓  Auto-replied to {rep_name} at {ts}")
        self.refresh()
        log.info("Auto-reply sent to %s at %s", rep_name, ts)

    def _on_auto_error(self, rep_name: str, msg: str) -> None:
        self.poll_status.setText(f"⚠  Auto-reply to {rep_name} failed: {msg[:80]}")
        log.warning("Auto-reply error for %s: %s", rep_name, msg)

    def _on_auto_done(self, worker: _AutoReplyWorker) -> None:
        try:
            self._auto_workers.remove(worker)
        except ValueError:
            pass

    def _on_poll_error(self, msg: str) -> None:
        self.poll_status.setText(f"⚠  IMAP error: {msg}")

    def _on_poll_done(self, worker: _ImapPollWorker) -> None:
        self.poll_btn.setEnabled(bool(self._cfg.email.imap_host and self._cfg.email.imap_username))
        try:
            self._poll_workers.remove(worker)
        except ValueError:
            pass

