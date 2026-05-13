"""AI provider settings dialog. API key stored in Windows Credential Manager."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from app.ai.factory import build_provider
from app.config.models import AIConfig
from app.config.store import get_secret, set_secret
from app.ui.theme import DANGER, SUCCESS, TEXT_MUTED


PROVIDER_CHOICES = [
    ("openai", "OpenAI", "GPT-4.1 / GPT-5 family. Strong general performance, reliable structured output."),
    ("anthropic", "Anthropic Claude", "Sonnet/Opus. Excellent at long context and careful tone (coming soon)."),
    ("azure_openai", "Azure OpenAI", "Same models as OpenAI billed via Azure tenant (coming soon)."),
]


class AISettingsDialog(QDialog):
    def __init__(self, cfg: AIConfig, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI Provider Settings")
        self.setMinimumWidth(520)
        self._cfg = cfg.model_copy(deep=True)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(12)

        intro = QLabel(
            "AI is used to draft and reply to coaching emails. The API key is stored in "
            "Windows Credential Manager — never in plain text on disk."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {TEXT_MUTED};")
        root.addWidget(intro)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        self.provider = QComboBox()
        for key, label, _desc in PROVIDER_CHOICES:
            self.provider.addItem(label, key)
        idx = max(0, self.provider.findData(self._cfg.provider))
        self.provider.setCurrentIndex(idx)

        self.provider_desc = QLabel(self._desc_for(self._cfg.provider))
        self.provider_desc.setStyleSheet(f"color: {TEXT_MUTED};")
        self.provider_desc.setWordWrap(True)
        self.provider.currentIndexChanged.connect(
            lambda _i: self.provider_desc.setText(self._desc_for(self.provider.currentData()))
        )

        self.model = QLineEdit(self._cfg.model)
        self.model.setPlaceholderText("e.g. gpt-4.1-mini, gpt-5, claude-sonnet-4-...")

        self.base_url = QLineEdit(self._cfg.base_url)
        self.base_url.setPlaceholderText("(blank for provider default)")

        self.api_username = QLineEdit(self._cfg.api_username)
        self.api_username.setPlaceholderText("Label for this key (e.g. 'default', 'work')")

        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        existing = get_secret("AI", f"{self._cfg.provider}:{self._cfg.api_username}")
        if existing:
            self.api_key.setPlaceholderText("(unchanged — leave blank to keep existing)")
        else:
            self.api_key.setPlaceholderText("Paste API key — stored in Windows Credential Manager")

        self.timeout = QSpinBox()
        self.timeout.setRange(5, 600)
        self.timeout.setValue(self._cfg.request_timeout_seconds)

        self.max_tokens = QSpinBox()
        self.max_tokens.setRange(64, 16000)
        self.max_tokens.setValue(self._cfg.max_output_tokens)

        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0.0, 2.0)
        self.temperature.setSingleStep(0.05)
        self.temperature.setDecimals(2)
        self.temperature.setValue(self._cfg.temperature)

        form.addRow("Provider", self.provider)
        form.addRow("", self.provider_desc)
        form.addRow("Model", self.model)
        form.addRow("Base URL", self.base_url)
        form.addRow("Key label", self.api_username)
        form.addRow("API key", self.api_key)
        form.addRow("Request timeout (s)", self.timeout)
        form.addRow("Max output tokens", self.max_tokens)
        form.addRow("Temperature", self.temperature)
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

    def _desc_for(self, key: str) -> str:
        for k, _label, desc in PROVIDER_CHOICES:
            if k == key:
                return desc
        return ""

    def _collect(self) -> AIConfig:
        return AIConfig(
            provider=self.provider.currentData(),
            model=self.model.text().strip() or "gpt-4.1-mini",
            base_url=self.base_url.text().strip(),
            api_username=self.api_username.text().strip() or "default",
            request_timeout_seconds=int(self.timeout.value()),
            max_output_tokens=int(self.max_tokens.value()),
            temperature=float(self.temperature.value()),
        )

    def commit_secrets(self) -> None:
        cfg = self._collect()
        key = self.api_key.text().strip()
        if key:
            set_secret("AI", f"{cfg.provider}:{cfg.api_username}", key)
            self.api_key.clear()

    def result_config(self) -> AIConfig:
        return self._collect()

    def _on_test(self) -> None:
        cfg = self._collect()
        pw = self.api_key.text().strip()
        if pw:
            set_secret("AI", f"{cfg.provider}:{cfg.api_username}", pw)
        self.test_btn.setEnabled(False)
        self.test_result.setText("Testing…")
        try:
            provider = build_provider(cfg)
            ok, msg = provider.ping()
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"{type(exc).__name__}: {exc}"
        color = SUCCESS if ok else DANGER
        self.test_result.setText(f"<span style='color:{color}'>{msg}</span>")
        self.test_btn.setEnabled(True)
