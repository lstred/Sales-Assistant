"""Conversations view (placeholder until conversation persistence is wired)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from app.ui.theme import TEXT_MUTED
from app.ui.views._header import ViewHeader


class ConversationsView(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)

        root.addWidget(
            ViewHeader(
                "Conversations",
                "Every email thread the assistant has opened with a rep, plus their replies and any commitments captured.",
            )
        )

        empty = QFrame()
        empty.setObjectName("card")
        empty_layout = QVBoxLayout(empty)
        empty_layout.setContentsMargins(40, 60, 40, 60)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("No conversations yet")
        title.setStyleSheet("font-size: 16px; font-weight: 600;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg = QLabel(
            "Configure email under Settings, then generate a manual review email "
            "from the Reps tab to start your first thread."
        )
        msg.setStyleSheet(f"color: {TEXT_MUTED};")
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg.setWordWrap(True)
        empty_layout.addWidget(title)
        empty_layout.addWidget(msg)

        root.addWidget(empty)
        root.addStretch(1)
