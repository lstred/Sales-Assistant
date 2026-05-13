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
    QVBoxLayout,
)

from app.config.models import EmailConfig
from app.config.store import delete_secret, get_secret, set_secret
from app.notifications.email_client import EmailClient
from app.ui.theme import DANGER, SUCCESS, TEXT_MUTED


def _password_field() -> QLineEdit:
    e = QLineEdit()
    e.setEchoMode(QLineEdit.EchoMode.Password)
    e.setPlaceholderText("(stored in Windows Credential Manager)")
    return e


class EmailSettingsDialog(QDialog):
    def __init__(self, cfg: EmailConfig, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Email Settings")
        self.setMinimumWidth(560)
        self._cfg = cfg.model_copy(deep=True)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(12)

        intro = QLabel(
            "SMTP is used to send drafts; IMAP picks up rep replies. "
            "Passwords are stored securely in Windows Credential Manager — never on disk. "
            "Outbound sending stays disabled until you flip 'Enable outbound send'."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {TEXT_MUTED};")
        root.addWidget(intro)

        # ---------------- SMTP group ----------------
        smtp_group = QGroupBox("SMTP (outbound)")
        smtp_form = QFormLayout(smtp_group)
        smtp_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        smtp_form.setHorizontalSpacing(14)
        smtp_form.setVerticalSpacing(10)

        self.smtp_host = QLineEdit(self._cfg.smtp_host)
        self.smtp_port = QSpinBox()
        self.smtp_port.setRange(1, 65535)
        self.smtp_port.setValue(self._cfg.smtp_port)
        self.smtp_starttls = QCheckBox("Use STARTTLS")
        self.smtp_starttls.setChecked(self._cfg.smtp_starttls)
        self.smtp_username = QLineEdit(self._cfg.smtp_username)
        self.smtp_password = _password_field()
        existing_smtp = (
            get_secret("SMTP", self._cfg.smtp_username) if self._cfg.smtp_username else None
        )
        if existing_smtp:
            self.smtp_password.setPlaceholderText("(unchanged — leave blank to keep existing)")
        self.smtp_from_name = QLineEdit(self._cfg.smtp_from_name)
        self.smtp_from_address = QLineEdit(self._cfg.smtp_from_address)

        smtp_form.addRow("Host", self.smtp_host)
        smtp_form.addRow("Port", self.smtp_port)
        smtp_form.addRow("", self.smtp_starttls)
        smtp_form.addRow("Username", self.smtp_username)
        smtp_form.addRow("Password", self.smtp_password)
        smtp_form.addRow("From name", self.smtp_from_name)
        smtp_form.addRow("From address", self.smtp_from_address)

        smtp_test_row = QHBoxLayout()
        self.smtp_test_btn = QPushButton("Test SMTP")
        self.smtp_test_btn.clicked.connect(self._on_test_smtp)
        smtp_test_row.addWidget(self.smtp_test_btn)
        smtp_test_row.addStretch(1)
        smtp_form.addRow("", _row(smtp_test_row))
        self.smtp_test_result = QLabel("")
        smtp_form.addRow("", self.smtp_test_result)

        # ---------------- IMAP group ----------------
        imap_group = QGroupBox("IMAP (inbound)")
        imap_form = QFormLayout(imap_group)
        imap_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        imap_form.setHorizontalSpacing(14)
        imap_form.setVerticalSpacing(10)

        self.imap_host = QLineEdit(self._cfg.imap_host)
        self.imap_port = QSpinBox()
        self.imap_port.setRange(1, 65535)
        self.imap_port.setValue(self._cfg.imap_port)
        self.imap_ssl = QCheckBox("Use SSL")
        self.imap_ssl.setChecked(self._cfg.imap_ssl)
        self.imap_username = QLineEdit(self._cfg.imap_username)
        self.imap_password = _password_field()
        existing_imap = (
            get_secret("IMAP", self._cfg.imap_username) if self._cfg.imap_username else None
        )
        if existing_imap:
            self.imap_password.setPlaceholderText("(unchanged — leave blank to keep existing)")
        self.imap_mailbox = QLineEdit(self._cfg.imap_mailbox)

        imap_form.addRow("Host", self.imap_host)
        imap_form.addRow("Port", self.imap_port)
        imap_form.addRow("", self.imap_ssl)
        imap_form.addRow("Username", self.imap_username)
        imap_form.addRow("Password", self.imap_password)
        imap_form.addRow("Mailbox", self.imap_mailbox)

        imap_test_row = QHBoxLayout()
        self.imap_test_btn = QPushButton("Test IMAP")
        self.imap_test_btn.clicked.connect(self._on_test_imap)
        imap_test_row.addWidget(self.imap_test_btn)
        imap_test_row.addStretch(1)
        imap_form.addRow("", _row(imap_test_row))
        self.imap_test_result = QLabel("")
        imap_form.addRow("", self.imap_test_result)

        # ---------------- Safety group ----------------
        safety_group = QGroupBox("Safety")
        safety_form = QFormLayout(safety_group)
        safety_form.setHorizontalSpacing(14)
        safety_form.setVerticalSpacing(10)
        self.enable_outbound = QCheckBox("Enable outbound send (default OFF — manual review only)")
        self.enable_outbound.setChecked(self._cfg.enable_outbound_send)
        self.redirect_all_to = QLineEdit(self._cfg.redirect_all_to)
        self.redirect_all_to.setPlaceholderText("Optional: redirect ALL outbound mail to this address (dry-run)")
        safety_form.addRow("", self.enable_outbound)
        safety_form.addRow("Redirect to", self.redirect_all_to)

        root.addWidget(smtp_group)
        root.addWidget(imap_group)
        root.addWidget(safety_group)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Save).setProperty("primary", True)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    # ------------------------------------------------------------ collection
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
        # SMTP
        smtp_pw = self.smtp_password.text()
        if smtp_pw:
            if cfg.smtp_username:
                set_secret("SMTP", cfg.smtp_username, smtp_pw)
            self.smtp_password.clear()
        # If username changed and old one had no value, optionally clear stale.
        # IMAP
        imap_pw = self.imap_password.text()
        if imap_pw:
            if cfg.imap_username:
                set_secret("IMAP", cfg.imap_username, imap_pw)
            self.imap_password.clear()

    def result_config(self) -> EmailConfig:
        return self._collect()

    # ---------------------------------------------------------------- tests
    def _on_test_smtp(self) -> None:
        cfg = self._collect()
        # Temporarily store password if provided, so test works
        pw = self.smtp_password.text()
        if pw and cfg.smtp_username:
            set_secret("SMTP", cfg.smtp_username, pw)
        self.smtp_test_btn.setEnabled(False)
        self.smtp_test_result.setText("Testing SMTP…")
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
        self.imap_test_result.setText("Testing IMAP…")
        ok, msg = EmailClient(cfg).test_imap()
        color = SUCCESS if ok else DANGER
        self.imap_test_result.setText(f"<span style='color:{color}'>{msg}</span>")
        self.imap_test_btn.setEnabled(True)


def _row(layout) -> "QWidget":  # noqa: F821 — Qt forward ref via local import
    from PySide6.QtWidgets import QWidget

    w = QWidget()
    w.setLayout(layout)
    return w
