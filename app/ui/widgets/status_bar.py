"""Custom status bar with three connection indicators (DB / Email / AI)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QStatusBar

from app.ui.theme import BORDER, DANGER, SUCCESS, TEXT_MUTED, WARN


class _Indicator(QFrame):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(6)
        self._dot = QLabel("●")
        self._dot.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 14px;")
        self._label = QLabel(f"{name}: unknown")
        self._label.setStyleSheet(f"color: {TEXT_MUTED};")
        layout.addWidget(self._dot)
        layout.addWidget(self._label)
        self._name = name

    def set_state(self, state: str, message: str = "") -> None:
        """state ∈ {ok, warn, error, unknown}."""
        color = {
            "ok": SUCCESS,
            "warn": WARN,
            "error": DANGER,
            "unknown": TEXT_MUTED,
        }.get(state, TEXT_MUTED)
        self._dot.setStyleSheet(f"color: {color}; font-size: 14px;")
        self._label.setText(f"{self._name}: {message or state}")


class AppStatusBar(QStatusBar):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSizeGripEnabled(False)

        self.db_indicator = _Indicator("Database")
        self.email_indicator = _Indicator("Email")
        self.ai_indicator = _Indicator("AI")

        # Pad with thin separators
        for w in (self.db_indicator, self._sep(), self.email_indicator, self._sep(), self.ai_indicator):
            self.addPermanentWidget(w)

    @staticmethod
    def _sep() -> QFrame:
        s = QFrame()
        s.setFrameShape(QFrame.Shape.VLine)
        s.setStyleSheet(f"color: {BORDER};")
        s.setFixedHeight(16)
        s.setAlignment = Qt.AlignmentFlag.AlignVCenter  # type: ignore[attr-defined]
        return s
