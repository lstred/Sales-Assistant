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
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSplitter,
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
    find_conversation_for_reply,
    list_action_items,
    list_conversations,
    list_messages,
    record_inbound,
    resolve_action_item,
    save_message,
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

    def _generate(self) -> str:
        from app.ai.base import ChatMessage
        from app.ai.factory import build_provider

        provider = build_provider(self._cfg.ai)

        inbound = [m for m in self._messages if m.direction == "inbound"]
        last_body = ""
        if inbound:
            last = inbound[-1]
            last_body = last.body_text or (self._strip_html(last.body_html) if last.body_html else "")

        # Always load the full rep dashboard from the warehouse.
        is_mgmt = self._is_management_sender()

        if is_mgmt:
            # Management sender — load company-wide data across all reps.
            warehouse_data = self._fetch_management_data()
            all_text = " ".join(
                (m.body_text or self._strip_html(m.body_html or "")) for m in self._messages
            )
            account_numbers = self._extract_account_numbers(all_text)
            account_detail = self._fetch_account_detail(account_numbers)
            warehouse_block = "\n\n".join(x for x in [warehouse_data, account_detail] if x)
        else:
            # Rep sender — scope to this rep's territory only.
            rep_data = self._fetch_rep_data()
            all_text = " ".join(
                (m.body_text or self._strip_html(m.body_html or "")) for m in self._messages
            )
            account_numbers = self._extract_account_numbers(all_text)
            account_detail = self._fetch_account_detail(account_numbers)
            warehouse_block = "\n\n".join(x for x in [rep_data, account_detail] if x)

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

                "PROFESSIONAL FORMATTING RULES:\n"
                "- Lead with the direct answer (the table or figure). Context after, not before.\n"
                "- Use ASCII tables with a header row, dash separator, and aligned data columns.\n"
                "- Left-align names/descriptions, right-align all currency and percentage columns.\n"
                "- Include a TOTAL row when showing a multi-row breakdown.\n"
                "- 300 words maximum. If the answer is a table, the table IS the response.\n"
                "- Plain text only — no markdown, no asterisks, no bullet symbols.\n"
                "- Do NOT add a sign-off or closing pleasantry.\n"
                "- End with ONE specific follow-up offer the warehouse can actually deliver."
            )
        else:
            system_msg = (
                "You are an AI data assistant automatically responding to a sales rep's email "
                "on behalf of the sales manager. You have live warehouse data pulled directly from "
                "the sales database — always use it. NEVER invent any number.\n\n"

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
                "- If the rep asks for data 'by rep' for all reps: you have this rep's territory only. "
                "Say so in one sentence, then immediately deliver the product × account table instead.\n"
                "- Always cite the exact date range (e.g. 'Feb 1 – May 20, 2026 FY YTD').\n"
                "- Address the rep by first name once, at the start.\n\n"

                "PROFESSIONAL FORMATTING RULES:\n"
                "- Lead with the direct answer (table or figure), then 1-2 sentences of context.\n"
                "- ASCII tables: header row, dash separator line, aligned columns. "
                "Left-align names, right-align numbers. Include totals row where relevant.\n"
                "- 100–220 words maximum. Plain text only — no markdown, no asterisks.\n"
                "- Do NOT add a sign-off or closing pleasantry.\n"
                "- End with ONE specific follow-up offer the warehouse can actually deliver."
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
        user_msg += "\nDraft the reply now:"

        result = provider.complete(
            [
                ChatMessage(role="system", content=system_msg),
                ChatMessage(role="user", content=user_msg),
            ],
            model=self._cfg.ai.model,
            max_output_tokens=max(1024, self._cfg.ai.max_output_tokens),
            temperature=0.2,
            timeout_seconds=self._cfg.ai.request_timeout_seconds,
        )
        return result.text.strip()


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

