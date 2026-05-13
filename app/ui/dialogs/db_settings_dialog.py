"""Database connection settings dialog."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from app.config.models import DatabaseConfig
from app.data.db import ping
from app.ui.theme import DANGER, SUCCESS, TEXT_MUTED


class DatabaseSettingsDialog(QDialog):
    def __init__(self, db: DatabaseConfig, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Database Settings")
        self.setMinimumWidth(480)
        self._db = db.model_copy(deep=True)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(12)

        intro = QLabel(
            "SQL Server connection. Authentication uses Windows Trusted Connection — "
            "no password is stored on disk."
        )
        intro.setStyleSheet(f"color: {TEXT_MUTED};")
        intro.setWordWrap(True)
        root.addWidget(intro)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        self.driver = QLineEdit(self._db.driver)
        self.server = QLineEdit(self._db.server)
        self.database = QLineEdit(self._db.database)
        self.trusted = QCheckBox("Use Windows Trusted Connection")
        self.trusted.setChecked(self._db.trusted_connection)
        self.encrypt = QComboBox()
        self.encrypt.addItems(["yes", "no", "strict"])
        self.encrypt.setCurrentText(self._db.encrypt)

        form.addRow("ODBC driver", self.driver)
        form.addRow("Server", self.server)
        form.addRow("Database", self.database)
        form.addRow("Authentication", self.trusted)
        form.addRow("Encrypt", self.encrypt)
        root.addLayout(form)

        self.test_btn = QPushButton("Test connection")
        self.test_btn.clicked.connect(self._on_test)
        root.addWidget(self.test_btn)

        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        root.addWidget(self.test_result)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Save).setProperty("primary", True)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def _collect(self) -> DatabaseConfig:
        return DatabaseConfig(
            driver=self.driver.text().strip() or "ODBC Driver 18 for SQL Server",
            server=self.server.text().strip(),
            database=self.database.text().strip(),
            trusted_connection=self.trusted.isChecked(),
            encrypt=self.encrypt.currentText(),
        )

    def _on_test(self) -> None:
        self.test_btn.setEnabled(False)
        self.test_result.setText("Testing…")
        ok, msg = ping(self._collect())
        color = SUCCESS if ok else DANGER
        self.test_result.setText(f"<span style='color:{color}'>{msg}</span>")
        self.test_btn.setEnabled(True)

    def result_config(self) -> DatabaseConfig:
        return self._collect()
