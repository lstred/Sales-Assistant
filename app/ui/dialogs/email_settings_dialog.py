"""Email (SMTP + IMAP) settings dialog. Passwords go to Windows Credential Manager."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.config.models import EmailConfig
from app.config.store import get_secret, set_secret
from app.notifications.email_client import EmailClient
from app.ui.theme import DANGER, SUCCESS, TEXT_MUTED


# ── helpers ──────────────────────────────────────────────────────────────────

def _password_field(placeholder: str = "(stored in Windows Credential Manager)") -> QLineEdit:
    e = QLineEdit()
    e.setEchoMode(QLineEdit.EchoMode.Password)
    e.setPlaceholderText(placeholder)
    return e


def _inline_row(*widgets: QWidget, spacing: int = 6) -> QWidget:
    """Pack widgets into a single QWidget with an HBoxLayout (no margins)."""
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(spacing)
    for ww in widgets:
        h.addWidget(ww)
    return w


# ── dialog ───────────────────────────────────────────────────────────────────

class EmailSettingsDialog(QDialog):
    def __init__(self, cfg: EmailConfig, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Email Settings")
        self.setMinimumWidth(560)
        self._cfg = cfg.model_copy(deep=True)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 14)
        root.setSpacing(10)

        # ── one-line intro ────────────────────────────────────────────────
        intro = QLabel(
            "Passwords are saved in <b>Windows Credential Manager</b> — never on disk.  "
            "Outbound sending is disabled by default until you enable it below."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        root.addWidget(intro)

        # ── SMTP / IMAP tabs ──────────────────────────────────────────────
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.addTab(self._build_smtp_tab(), "  Outbound (SMTP)  ")
        tabs.addTab(self._build_imap_tab(), "  Inbound (IMAP)  ")
        root.addWidget(tabs, 1)

        # ── Safety (always visible — only 2 rows) ────────────────────────
        safety = QGroupBox("Safety")
        sf = QFormLayout(safety)
        sf.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        sf.setHorizontalSpacing(14)
        sf.setVerticalSpacing(8)
        sf.setContentsMargins(14, 10, 14, 10)

        self.enable_outbound = QCheckBox(
            "Enable outbound send  (default OFF — manual review only)"
        )
        self.enable_outbound.setChecked(self._cfg.enable_outbound_send)

        self.redirect_all_to = QLineEdit(self._cfg.redirect_all_to)
        self.redirect_all_to.setPlaceholderText(
            "Optional: redirect ALL outbound mail here for testing (dry-run)"
        )

        sf.addRow("", self.enable_outbound)
        sf.addRow("Redirect to", self.redirect_all_to)
        root.addWidget(safety)

        # ── buttons ───────────────────────────────────────────────────────
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        bb.button(QDialogButtonBox.StandardButton.Save).setProperty("primary", True)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    # ─────────────────────────────────────────── tab builders

    def _build_smtp_tab(self) -> QWidget:
        w = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(16, 14, 16, 14)
        vl.setSpacing(0)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(9)

        # Host + port + TLS on one line
        self.smtp_host = QLineEdit(self._cfg.smtp_host)
        self.smtp_host.setPlaceholderText("e.g. smtp.office365.com")
        self.smtp_port = QSpinBox()
        self.smtp_port.setRange(1, 65535)
        self.smtp_port.setValue(self._cfg.smtp_port)
        self.smtp_port.setFixedWidth(72)
        self.smtp_starttls = QCheckBox("STARTTLS")
        self.smtp_starttls.setChecked(self._cfg.smtp_starttls)
        self.smtp_port.valueChanged.connect(self._on_smtp_port_changed)

        form.addRow(
            "Host / Port",
            _inline_row(self.smtp_host, self.smtp_port, self.smtp_starttls),
        )

        self.smtp_username = QLineEdit(self._cfg.smtp_username)
        self.smtp_username.setPlaceholderText("your@email.com")
        form.addRow("Username", self.smtp_username)

        _existing_smtp = (
            get_secret("SMTP", self._cfg.smtp_username) if self._cfg.smtp_username else None
        )
        self.smtp_password = _password_field(
            "(unchanged — leave blank to keep existing)"
            if _existing_smtp
            else "(stored in Windows Credential Manager)"
        )
        form.addRow("Password", self.smtp_password)

        self.smtp_from_name = QLineEdit(self._cfg.smtp_from_name)
        self.smtp_from_name.setPlaceholderText("Sales Assistant")
        form.addRow("From name", self.smtp_from_name)

        self.smtp_from_address = QLineEdit(self._cfg.smtp_from_address)
        self.smtp_from_address.setPlaceholderText("address shown to recipients")
        form.addRow("From address", self.smtp_from_address)

        vl.addLayout(form)
        vl.addSpacing(12)

        # Test button + inline result
        self.smtp_test_btn = QPushButton("Test SMTP connection")
        self.smtp_test_btn.setFixedWidth(180)
        self.smtp_test_btn.clicked.connect(self._on_test_smtp)
        self.smtp_test_result = QLabel("")
        self.smtp_test_result.setWordWrap(True)
        test_row = QHBoxLayout()
        test_row.setSpacing(10)
        test_row.addWidget(self.smtp_test_btn)
        test_row.addWidget(self.smtp_test_result, 1)
        vl.addLayout(test_row)
        vl.addStretch(1)
        return w

    def _build_imap_tab(self) -> QWidget:
        w = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(16, 14, 16, 14)
        vl.setSpacing(0)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(9)

        self.imap_host = QLineEdit(self._cfg.imap_host)
        self.imap_host.setPlaceholderText("e.g. outlook.office365.com")
        self.imap_port = QSpinBox()
        self.imap_port.setRange(1, 65535)
        self.imap_port.setValue(self._cfg.imap_port)
        self.imap_port.setFixedWidth(72)
        self.imap_ssl = QCheckBox("SSL")
        self.imap_ssl.setChecked(self._cfg.imap_ssl)

        form.addRow(
            "Host / Port",
            _inline_row(self.imap_host, self.imap_port, self.imap_ssl),
        )

        self.imap_username = QLineEdit(self._cfg.imap_username)
        self.imap_username.setPlaceholderText("your@email.com")
        form.addRow("Username", self.imap_username)

        _existing_imap = (
            get_secret("IMAP", self._cfg.imap_username) if self._cfg.imap_username else None
        )
        self.imap_password = _password_field(
            "(unchanged — leave blank to keep existing)"
            if _existing_imap
            else "(stored in Windows Credential Manager)"
        )
        form.addRow("Password", self.imap_password)

        self.imap_mailbox = QLineEdit(self._cfg.imap_mailbox)
        self.imap_mailbox.setPlaceholderText("INBOX")
        form.addRow("Mailbox", self.imap_mailbox)

        vl.addLayout(form)
        vl.addSpacing(12)

        self.imap_test_btn = QPushButton("Test IMAP connection")
        self.imap_test_btn.setFixedWidth(180)
        self.imap_test_btn.clicked.connect(self._on_test_imap)
        self.imap_test_result = QLabel("")
        self.imap_test_result.setWordWrap(True)
        test_row = QHBoxLayout()
        test_row.setSpacing(10)
        test_row.addWidget(self.imap_test_btn)
        test_row.addWidget(self.imap_test_result, 1)
        vl.addLayout(test_row)
        vl.addStretch(1)
        return w

    # ─────────────────────────────────────────── smart defaults

    def _on_smtp_port_changed(self, port: int) -> None:
        """Auto-toggle STARTTLS based on well-known port numbers."""
        if port == 587:
            self.smtp_starttls.setChecked(True)
        elif port == 465:
            self.smtp_starttls.setChecked(False)

    # ─────────────────────────────────────────── collection

    def _collect(self) -> EmailConfig:
        return EmailConfig(
            smtp_host=self.smtp_host.text().strip(),
            smtp_port=int(self.smtp_port.value()),
            smtp_starttls=self.smtp_starttls.isChecked(),
            smtp_username=self.smtp_username.text().strip(),
            smtp_from_address=self.smtp_from_address.text().strip(),
            smtp_from_name=self.smtp_from_name.text().strip() or "Sales Assistant",
            imap_host=self.imap_host.text().strip(),
            imap_port=int(self.imap_port.value()),
            imap_ssl=self.imap_ssl.isChecked(),
            imap_username=self.imap_username.text().strip(),
            imap_mailbox=self.imap_mailbox.text().strip() or "INBOX",
            enable_outbound_send=self.enable_outbound.isChecked(),
            redirect_all_to=self.redirect_all_to.text().strip(),
        )

    def commit_secrets(self) -> None:
        """Persist any newly entered passwords to keyring."""
        cfg = self._collect()
        smtp_pw = self.smtp_password.text()
        if smtp_pw and cfg.smtp_username:
            set_secret("SMTP", cfg.smtp_username, smtp_pw)
            self.smtp_password.clear()
        imap_pw = self.imap_password.text()
        if imap_pw and cfg.imap_username:
            set_secret("IMAP", cfg.imap_username, imap_pw)
            self.imap_password.clear()

    def result_config(self) -> EmailConfig:
        return self._collect()

    # ─────────────────────────────────────────── connection tests

    def _on_test_smtp(self) -> None:
        cfg = self._collect()
        pw = self.smtp_password.text()
        if pw and cfg.smtp_username:
            set_secret("SMTP", cfg.smtp_username, pw)
        self.smtp_test_btn.setEnabled(False)
        self.smtp_test_result.setText("Testing…")
        ok, msg = EmailClient(cfg).test_smtp()
        color = SUCCESS if ok else DANGER
        self.smtp_test_result.setText(f"<span style='color:{color}'>{msg}</span>")
        self.smtp_test_btn.setEnabled(True)

    def _on_test_imap(self) -> None:
        cfg = self._collect()
        pw = self.imap_password.text()
        if pw and cfg.imap_username:
            set_secret("IMAP", cfg.imap_username, pw)
        self.imap_test_btn.setEnabled(False)
        self.imap_test_result.setText("Testing…")
        ok, msg = EmailClient(cfg).test_imap()
        color = SUCCESS if ok else DANGER
        self.imap_test_result.setText(f"<span style='color:{color}'>{msg}</span>")
        self.imap_test_btn.setEnabled(True)

