"""Conversations view — tracks every AI-originated email thread.

Shows:
- All conversations (grouped by status)
- Needs Review tab: inbound rep replies that haven't been responded to yet
- Action Items tab: extracted commitments from rep replies

On launch the view auto-loads from SQLite and optionally polls IMAP for new
rep replies. The sidebar badge is updated whenever the needs-review count
changes.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTextBrowser,
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
    resolve_action_item,
    save_message,
)
from app.ui.theme import ACCENT, BORDER, SURFACE, TEXT, TEXT_MUTED
from app.ui.views._header import ViewHeader

log = logging.getLogger(__name__)

_STATUS_COLORS = {
    "active": "#16A34A",
    "closed": "#94A3B8",
    "escalated": "#DC2626",
}


# ======================================================== background IMAP worker

class _ImapPollWorker(QThread):
    """Fetch unseen IMAP messages and save any recognised rep replies to SQLite."""

    found = Signal(int)   # number of new inbound messages saved
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
                    continue  # Message not part of any known thread — ignore
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


# ============================================================ conversations view

class ConversationsView(QWidget):
    """Full conversation management view with reply queue and action items."""

    needs_review_changed = Signal(int)  # emits count when it changes

    def __init__(self, cfg: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._conversations: list[Conversation] = []
        self._selected_conv: Conversation | None = None
        self._action_items: list[ActionItem] = []
        self._poll_workers: list[_ImapPollWorker] = []

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

        imap_configured = bool(cfg.email.imap_host and cfg.email.imap_username)
        if not imap_configured:
            self.poll_btn.setEnabled(False)
            self.poll_btn.setToolTip("Configure IMAP in Settings → Email to enable automatic reply detection.")
            self.poll_status.setText("IMAP not configured — configure in Settings → Email to detect rep replies automatically.")

        # Tabs: All Conversations | Needs Review | Action Items
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        # ---- Tab 0: All Conversations
        all_tab = QWidget()
        at_layout = QVBoxLayout(all_tab)
        at_layout.setContentsMargins(0, 8, 0, 0)
        at_layout.setSpacing(8)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: conversation list + filter buttons
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

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        lv.addWidget(self.refresh_btn)

        splitter.addWidget(left)

        # Right: message thread
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(8)

        self.thread_label = QLabel("")
        self.thread_label.setStyleSheet("font-weight: 600; font-size: 13px;")
        rv.addWidget(self.thread_label)

        self.thread_view = QTextBrowser()
        self.thread_view.setOpenExternalLinks(False)
        self.thread_view.setStyleSheet(
            f"QTextBrowser {{ background: {SURFACE}; border: 1px solid {BORDER};"
            f" border-radius: 8px; padding: 14px; color: {TEXT}; }}"
        )
        rv.addWidget(self.thread_view, 1)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 700])
        at_layout.addWidget(splitter, 1)
        self.tabs.addTab(all_tab, "All Conversations")

        # ---- Tab 1: Needs Review (unanswered rep replies)
        review_tab = QWidget()
        rev_layout = QVBoxLayout(review_tab)
        rev_layout.setContentsMargins(0, 8, 0, 0)
        rev_layout.setSpacing(8)

        self.review_banner = QLabel(
            "Rep replies that arrived while the app was closed — or that you "
            "haven't responded to yet. These stay here until you send a reply "
            "or mark them as handled."
        )
        self.review_banner.setWordWrap(True)
        self.review_banner.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 12px; padding: 6px 0;"
        )
        rev_layout.addWidget(self.review_banner)

        self.review_list = QListWidget()
        self.review_list.setAlternatingRowColors(True)
        self.review_list.itemSelectionChanged.connect(self._on_review_selected)
        rev_layout.addWidget(self.review_list, 1)

        self.review_detail = QTextBrowser()
        self.review_detail.setStyleSheet(
            f"QTextBrowser {{ background: {SURFACE}; border: 1px solid {BORDER};"
            f" border-radius: 8px; padding: 14px; color: {TEXT}; }}"
        )
        self.review_detail.setMaximumHeight(200)
        rev_layout.addWidget(self.review_detail)

        review_actions = QHBoxLayout()
        self.mark_replied_btn = QPushButton("Mark as replied (manual)")
        self.mark_replied_btn.setToolTip(
            "Records that you replied to this thread outside the app, "
            "so it stops appearing in the Needs Review queue."
        )
        self.mark_replied_btn.setEnabled(False)
        self.mark_replied_btn.clicked.connect(self._mark_replied)
        review_actions.addWidget(self.mark_replied_btn)
        review_actions.addStretch(1)
        self.review_status = QLabel("")
        self.review_status.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        review_actions.addWidget(self.review_status)
        rev_layout.addLayout(review_actions)

        self.tabs.addTab(review_tab, "Needs Review")

        # ---- Tab 2: Action Items
        actions_tab = QWidget()
        act_layout = QVBoxLayout(actions_tab)
        act_layout.setContentsMargins(0, 8, 0, 0)
        act_layout.setSpacing(8)

        act_hdr = QHBoxLayout()
        act_hdr.addWidget(
            QLabel("Open commitments extracted from rep replies:")
        )
        act_hdr.addStretch(1)
        self.show_done_btn = QPushButton("Show done")
        self.show_done_btn.setCheckable(True)
        self.show_done_btn.clicked.connect(self._refresh_action_list)
        act_hdr.addWidget(self.show_done_btn)
        act_layout.addLayout(act_hdr)

        self.action_list = QListWidget()
        self.action_list.setAlternatingRowColors(True)
        act_layout.addWidget(self.action_list, 1)

        action_btns = QHBoxLayout()
        self.mark_done_btn = QPushButton("Mark done")
        self.mark_done_btn.setEnabled(False)
        self.mark_done_btn.clicked.connect(self._mark_action_done)
        self.mark_skip_btn = QPushButton("Skip")
        self.mark_skip_btn.setEnabled(False)
        self.mark_skip_btn.clicked.connect(self._mark_action_skipped)
        action_btns.addWidget(self.mark_done_btn)
        action_btns.addWidget(self.mark_skip_btn)
        action_btns.addStretch(1)
        act_layout.addLayout(action_btns)
        self.action_list.itemSelectionChanged.connect(self._on_action_selected)

        self.tabs.addTab(actions_tab, "Action Items")

        root.addWidget(self.tabs, 1)

        # Auto-load on first show
        QTimer.singleShot(0, self.refresh)

    # ---------------------------------------------------------------- load
    def refresh(self) -> None:
        """Reload conversations and action items from SQLite."""
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
        self.tabs.setTabText(
            2, f"Action Items ({open_actions})" if open_actions else "Action Items"
        )
        self.needs_review_changed.emit(needs)
        # Tint the Needs Review tab red when there are pending items, reset otherwise.
        self.tabs.tabBar().setTabTextColor(1, QColor("#DC2626") if needs > 0 else QColor())

    # ---------------------------------------------------------------- all conversations tab
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
            label = (
                f"{rep}{badge}\n"
                f"  {c.subject[:60]}\n"
                f"  {c.last_activity_at[:16]}"
            )
            item = QListWidgetItem(label)
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
        self._selected_conv = conv
        self._load_thread(conv)

    def _load_thread(self, conv: Conversation) -> None:
        rep = conv.rep_name or conv.rep_id
        self.thread_label.setText(f"{rep} — {conv.subject}  [{conv.status}]")
        try:
            messages = list_messages(conv.id)
        except Exception:
            messages = []
        if not messages:
            self.thread_view.setHtml(
                f"<p style='color:{TEXT_MUTED}'>No messages recorded in this thread yet.</p>"
                "<p style='color:#64748B;font-size:12px;'>Messages will appear "
                "here once you send emails via the Weekly Email tab and reps reply.</p>"
            )
            return
        html_parts = []
        for msg in messages:
            is_in = msg.direction == "inbound"
            bg = "#EFF6FF" if is_in else "#F0FDF4"
            who = f"From: {msg.from_address}" if is_in else f"To: {msg.to_address}"
            header = (
                f"<div style='font-size:11px;color:{TEXT_MUTED};margin-bottom:6px;'>"
                f"{'← Rep reply' if is_in else '→ Sent'} · {msg.sent_at[:16]} · {who}"
                f"</div>"
            )
            # Prefer HTML body; fall back to escaped plain text
            if msg.body_html:
                body_content = msg.body_html
            else:
                escaped = (msg.body_text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                body_content = f"<div style='white-space:pre-wrap;font-size:13px;'>{escaped[:4000]}</div>"
            html_parts.append(
                f"<div style='background:{bg};border-radius:8px;padding:10px 14px;"
                f"margin:8px 0;border:1px solid {'#BFDBFE' if is_in else '#BBF7D0'};'>"
                + header + body_content
                + "</div>"
            )
        self.thread_view.setHtml("".join(html_parts))

    # ---------------------------------------------------------------- needs review tab
    def _populate_review_list(self) -> None:
        self.review_list.clear()
        needs = [c for c in self._conversations if c.needs_reply]
        if not needs:
            item = QListWidgetItem("✓  All caught up — no unanswered replies.")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            item.setForeground(Qt.GlobalColor.gray)
            self.review_list.addItem(item)
            self.mark_replied_btn.setEnabled(False)
            return
        for c in needs:
            rep = c.rep_name or c.rep_id
            label = (
                f"🔴  {rep}\n"
                f"     {c.subject[:60]}\n"
                f"     Last reply: {c.last_inbound_at[:16] if c.last_inbound_at else '?'}"
            )
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, c.id)
            self.review_list.addItem(item)

    def _on_review_selected(self) -> None:
        items = self.review_list.selectedItems()
        if not items:
            self.mark_replied_btn.setEnabled(False)
            self.review_detail.clear()
            return
        conv_id = items[0].data(Qt.ItemDataRole.UserRole)
        if not isinstance(conv_id, int):
            self.mark_replied_btn.setEnabled(False)
            return
        self.mark_replied_btn.setEnabled(True)
        conv = next((c for c in self._conversations if c.id == conv_id), None)
        if conv is None:
            return
        try:
            messages = list_messages(conv.id)
        except Exception:
            messages = []
        # Show the most recent inbound message body
        inbound = [m for m in messages if m.direction == "inbound"]
        if inbound:
            last = inbound[-1]
            if last.body_html:
                body_html = last.body_html
            else:
                escaped = (last.body_text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                body_html = f"<div style='white-space:pre-wrap;font-size:13px;'>{escaped[:3000]}</div>"
            self.review_detail.setHtml(
                f"<p style='font-size:11px;color:{TEXT_MUTED};margin-bottom:6px;'>"
                f"From: {last.from_address} · {last.sent_at[:16]}</p>"
                + body_html
            )
        else:
            self.review_detail.clear()

    def _mark_replied(self) -> None:
        """Log a manual outbound reply so the thread clears the review queue."""
        items = self.review_list.selectedItems()
        if not items:
            return
        conv_id = items[0].data(Qt.ItemDataRole.UserRole)
        if not isinstance(conv_id, int):
            return
        try:
            conv = next((c for c in self._conversations if c.id == conv_id), None)
            if conv:
                # Use the rep's configured email address if available, else a placeholder
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

    # ---------------------------------------------------------------- action items tab
    def _refresh_action_list(self) -> None:
        self.action_list.clear()
        show_done = self.show_done_btn.isChecked()
        try:
            items = list_action_items(status=None if show_done else "open")
        except Exception:
            items = []
        if not items:
            placeholder = QListWidgetItem(
                "No open action items.\n\n"
                "Action items are extracted from rep replies when email "
                "transport is active (e.g. 'I'll call them Friday')."
            )
            placeholder.setFlags(placeholder.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            placeholder.setForeground(Qt.GlobalColor.gray)
            self.action_list.addItem(placeholder)
            self.mark_done_btn.setEnabled(False)
            self.mark_skip_btn.setEnabled(False)
            return
        for ai in items:
            due = f"  Due: {ai.due_at}" if ai.due_at else ""
            status_icon = {"open": "○", "done": "✓", "skipped": "—"}.get(ai.status, "?")
            label = f"{status_icon}  [{ai.rep_id}] {ai.description[:80]}{due}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, ai.id)
            if ai.status != "open":
                item.setForeground(Qt.GlobalColor.gray)
            self.action_list.addItem(item)

    def _on_action_selected(self) -> None:
        items = self.action_list.selectedItems()
        has = bool(items and isinstance(items[0].data(Qt.ItemDataRole.UserRole), int))
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

    # ---------------------------------------------------------------- IMAP polling
    def _poll_imap(self) -> None:
        """Kick off a background IMAP poll for new rep replies."""
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

