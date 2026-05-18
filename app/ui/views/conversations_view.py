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
                    if not whitelist or from_email not in whitelist:
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

            # ── Top products ──────────────────────────────────────────────
            pc_cur = (
                df_c.groupby("price_class", dropna=False)
                .agg(revenue=("revenue", "sum"), gp=("gross_profit", "sum"))
                .reset_index()
                .sort_values("revenue", ascending=False)
                .head(15)
            )
            prior_pc: dict[str, float] = (
                df_p.groupby("price_class")["revenue"].sum().to_dict()
                if not df_p.empty
                else {}
            )

            lines.append(f"TOP PRODUCTS BY REVENUE (FY {fy} YTD vs prior FY YTD):")
            lines.append(f"  {'Product':<30} {'YTD $':>12} {'Prev YTD $':>12} {'YoY':>8} {'GP%':>6}")
            lines.append(f"  {'-'*30} {'-'*12} {'-'*12} {'-'*8} {'-'*6}")
            for _, row in pc_cur.iterrows():
                pc = str(row["price_class"]).strip()
                desc = pc_lookup.get(pc, pc)[:30]
                rev = row["revenue"]
                gp = row["gp"]
                gp_pct = gp / rev * 100 if rev > 0 else 0.0
                prev = prior_pc.get(pc, 0.0)
                yoy = f"{(rev - prev) / prev * 100:+.1f}%" if prev > 0 else "new"
                prev_s = f"${prev:,.0f}" if prev > 0 else "—"
                lines.append(
                    f"  {desc:<30} ${rev:>11,.0f} {prev_s:>12} {yoy:>8} {gp_pct:>5.1f}%"
                )
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
        rep_data = self._fetch_rep_data()

        # Additionally fetch month-by-month detail for any accounts explicitly named.
        all_text = " ".join(
            (m.body_text or self._strip_html(m.body_html or "")) for m in self._messages
        )
        account_numbers = self._extract_account_numbers(all_text)
        account_detail = self._fetch_account_detail(account_numbers)

        warehouse_block = "\n\n".join(x for x in [rep_data, account_detail] if x)

        system_msg = (
            "You are an AI data assistant automatically responding to a sales rep's email "
            "on behalf of the sales manager. You have access to live warehouse data pulled "
            "directly from the sales database — always use it.\n\n"
            "CRITICAL RULES — violating any of these is a failure:\n"
            "- NEVER invent, estimate, or fabricate any number. If a specific figure is not "
            "in FRESH WAREHOUSE DATA, say exactly: 'I don\\'t have that breakdown in the "
            "warehouse right now — I can pull [X] if you want.' Never guess.\n"
            "- Use the WAREHOUSE DATA product names and account names verbatim. Do not rename them.\n"
            "- Always cite the exact date range the numbers cover (e.g. 'Feb 2, 2026 – May 18, 2026').\n"
            "- Address the rep by first name once, at the very start.\n"
            "- If the rep's request is clear and the data is present: respond with the specific "
            "table or figures from FRESH WAREHOUSE DATA. Keep it 100–220 words.\n"
            "- If the rep's request is AMBIGUOUS or the specific data they need is NOT in the "
            "warehouse block: start with exactly 'CLARIFICATION NEEDED:' and ask ONE question.\n"
            "- Plain text and simple ASCII tables only — no markdown, no asterisks, no bullet "
            "symbols. Use dashes for table borders.\n"
            "- Do NOT add a sign-off, signature, or closing pleasantry.\n"
            "- Close with ONE offer of a follow-up data pull the warehouse can actually deliver "
            "(e.g. a specific account breakdown, GP% trend, product-line detail). Never suggest "
            "calls, meetings, or actions you cannot take as an AI data tool."
        )

        user_msg = (
            f"Rep: {self._conv.rep_name or self._conv.rep_id}\n"
            f"Subject: {self._conv.subject}\n\n"
            f"CONVERSATION HISTORY:\n{self._history_text()}\n\n"
            f"REP'S LATEST MESSAGE:\n{last_body[:1500]}\n"
        )
        if warehouse_block:
            user_msg += f"\nFRESH WAREHOUSE DATA (use this — do not invent anything outside it):\n{warehouse_block}\n"
        else:
            user_msg += (
                "\nWARNING: Warehouse data could not be loaded for this rep. "
                "Do NOT invent any numbers. Tell the rep the data pull failed and "
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
            res = client.send(
                to_address=rep_email,
                subject=subj,
                body_text=draft,
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

        client = EmailClient(self._cfg.email)
        res = client.send(
            to_address=self._rep_email,
            subject=subj,
            body_text=body,
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
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("save_message error after reply: %s", exc)

        self.sent.emit()
        self.accept()


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
            if wl_count:
                self.poll_status.setText(
                    f"Auto-reply active for {wl_count} whitelisted address(es) — checking every 2 min."
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
            # Only addresses explicitly whitelisted receive auto-replies.
            # An empty whitelist means auto-reply is inactive for everyone.
            whitelist = {
                e.strip().lower()
                for e in (self._cfg.email.auto_reply_whitelist or [])
                if e.strip()
            }
            if not whitelist:
                log.info(
                    "Auto-reply skipped for %s — whitelist is empty (add addresses "
                    "in Email Settings → Auto-Reply tab)",
                    rep_email,
                )
                continue
            if rep_email.lower() not in whitelist:
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

