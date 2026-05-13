"""Settings landing view: launches the dedicated settings dialogs."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.ui.theme import TEXT_MUTED
from app.ui.views._header import ViewHeader


class _SettingsCard(QFrame):
    clicked = Signal()

    def __init__(self, title: str, description: str, button_text: str) -> None:
        super().__init__()
        self.setObjectName("card")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(8)

        t = QLabel(title)
        t.setStyleSheet("font-size: 14px; font-weight: 600;")
        d = QLabel(description)
        d.setStyleSheet(f"color: {TEXT_MUTED};")
        d.setWordWrap(True)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn = QPushButton(button_text)
        self.btn.setProperty("primary", True)
        self.btn.clicked.connect(self.clicked.emit)
        btn_row.addWidget(self.btn)

        layout.addWidget(t)
        layout.addWidget(d)
        layout.addLayout(btn_row)


class SettingsView(QWidget):
    open_db = Signal()
    open_email = Signal()
    open_ai = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(16)

        root.addWidget(
            ViewHeader(
                "Settings",
                "Connection details, credentials, and provider configuration. Secrets are stored in Windows Credential Manager — never on disk.",
            )
        )

        grid = QGridLayout()
        grid.setSpacing(16)

        db_card = _SettingsCard(
            "Database",
            "SQL Server warehouse used for rep, account and sales data. Uses Windows Trusted Connection.",
            "Configure database",
        )
        db_card.clicked.connect(self.open_db.emit)

        email_card = _SettingsCard(
            "Email (SMTP + IMAP)",
            "Outbound (SMTP) and inbound (IMAP) servers used to send drafts and pick up rep replies.",
            "Configure email",
        )
        email_card.clicked.connect(self.open_email.emit)

        ai_card = _SettingsCard(
            "AI Provider",
            "Language model used to draft and reply to coaching emails. Currently supports OpenAI; abstraction allows others later.",
            "Configure AI",
        )
        ai_card.clicked.connect(self.open_ai.emit)

        grid.addWidget(db_card, 0, 0)
        grid.addWidget(email_card, 0, 1)
        grid.addWidget(ai_card, 1, 0)

        root.addLayout(grid)
        root.addStretch(1)
