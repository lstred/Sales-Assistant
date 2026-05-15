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
    find_conversation_for_reply,
    list_action_items,
    list_conversations,
    list_messages,
    record_inbound,
    record_send,
    resolve_action_item,
    save_message,
)
from app.ui.theme import ACCENT, BORDER, DANGER, SURFACE, SURFACE_ALT, TEXT, TEXT_MUTED
from app.ui.views._header import ViewHeader

log = logging.getLogger(__name__)


# ================================================================ IMAP poll worker

class _ImapPollWorker(QThread):
    """Fetch unseen IMAP messages and match them to existing threads."""

    found = Signal(int)
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
            for reply in replies:
                conv = find_conversation_for_reply(
                    reply["in_reply_to"],
                    reply["references"],
                )
                if conv is None:
                    continue
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
            self.found.emit(saved)
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

    def _fetch_account_data(self, account_numbers: list[str]) -> str:
        """Monthly invoiced sales for the requested accounts."""
        if not account_numbers or not self._get_db:
            return ""
        try:
            from datetime import date
            from app.data.loaders import load_invoiced_sales, load_rep_assignments
            db = self._get_db()
            if db is None:
                return ""
            end = date.today()
            start = date(end.year - 1, 1, 1)
            df = load_invoiced_sales(db, start, end)
            if df is None or df.empty:
                return ""
            df = df[df["account_number"].astype(str).isin(account_numbers)].copy()
            if df.empty:
                return ""

            # Account name lookup
            acct_names: dict[str, str] = {}
            try:
                asgn = load_rep_assignments(db)
                if "account_number" in asgn.columns and "account_name" in asgn.columns:
                    for _, row in asgn[asgn["account_number"].astype(str).isin(account_numbers)].iterrows():
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

            lines: list[str] = []
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
            log.warning("Account data fetch failed: %s", exc)
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

        all_text = " ".join(
            (m.body_text or self._strip_html(m.body_html or "")) for m in self._messages
        )
        account_numbers = self._extract_account_numbers(all_text)
        sales_data = self._fetch_account_data(account_numbers)

        system_msg = (
            "You are helping a sales manager reply to a rep's email response. "
            "The rep replied to a data-rich coaching email that may have included a service offer "
            "(e.g. 'Reply YES for a month-by-month breakdown'). "
            "Read the full conversation history, identify exactly what the rep requested, "
            "and write a professional, data-rich reply fulfilling that request.\n\n"
            "Rules:\n"
            "- Address the rep by first name once, at the start.\n"
            "- If month-by-month or account data was requested, present it as a clean bullet list or table.\n"
            "- Cite specific account names and dollar figures from the data provided.\n"
            "- Keep it 100–220 words. Write plain paragraphs only — no markdown, no asterisks.\n"
            "- Do NOT add a sign-off or signature.\n"
            "- End with ONE forward-looking action prompt.\n"
            "- Never invent numbers not present in the supplied data."
        )

        user_msg = (
            f"Rep: {self._conv.rep_name or self._conv.rep_id}\n"
            f"Subject: {self._conv.subject}\n\n"
            f"CONVERSATION HISTORY:\n{self._history_text()}\n\n"
            f"REP'S LATEST MESSAGE:\n{last_body[:1500]}\n"
        )
        if sales_data:
            user_msg += f"\nFRESH WAREHOUSE DATA:\n{sales_data}\n"
        user_msg += "\nDraft the reply now:"

        result = provider.complete(
            [
                ChatMessage(role="system", content=system_msg),
                ChatMessage(role="user", content=user_msg),
            ],
            model=self._cfg.ai.model,
            max_output_tokens=max(1024, self._cfg.ai.max_output_tokens),
            temperature=0.35,
            timeout_seconds=self._cfg.ai.request_timeout_seconds,
        )
        return result.text.strip()


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
            record_send(
                salesman_number=self._conv.rep_id,
                rep_name=self._conv.rep_name or self._conv.rep_id,
                subject=subj,
                thread_key=res.message_id,
                from_address=self._cfg.email.smtp_from_address or "(manager)",
                to_address=self._rep_email,
                body_html="",
                cost_center=self._conv.cost_center or "",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("record_send error after reply: %s", exc)

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
        if not imap_ok:
            self.poll_btn.setEnabled(False)
            self.poll_btn.setToolTip("Configure IMAP in Settings → Email to enable automatic reply detection.")
            self.poll_status.setText("IMAP not configured — configure in Settings → Email.")

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
        self.poll_btn.setEnabled(False)
        self.poll_status.setText("Checking inbox…")
        worker = _ImapPollWorker(self._cfg, parent=self)
        self._poll_workers.append(worker)
        worker.found.connect(self._on_poll_found)
        worker.error.connect(self._on_poll_error)
        worker.done.connect(lambda: self._on_poll_done(worker))
        worker.start()

    def _on_poll_found(self, count: int) -> None:
        if count:
            self.poll_status.setText(f"✓  {count} new repl{'y' if count == 1 else 'ies'} saved.")
            self.refresh()
        else:
            self.poll_status.setText("✓  No new replies found.")

    def _on_poll_error(self, msg: str) -> None:
        self.poll_status.setText(f"⚠  IMAP error: {msg}")

    def _on_poll_done(self, worker: _ImapPollWorker) -> None:
        self.poll_btn.setEnabled(bool(self._cfg.email.imap_host and self._cfg.email.imap_username))
        try:
            self._poll_workers.remove(worker)
        except ValueError:
            pass

