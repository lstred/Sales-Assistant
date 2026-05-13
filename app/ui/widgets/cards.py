"""KPI / summary card widget."""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QVBoxLayout


class KpiCard(QFrame):
    def __init__(self, title: str, value: str = "—", caption: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(6)

        self._title = QLabel(title)
        self._title.setObjectName("cardTitle")

        self._value = QLabel(value)
        self._value.setObjectName("cardValue")

        self._caption = QLabel(caption)
        self._caption.setObjectName("cardCaption")
        self._caption.setVisible(bool(caption))

        layout.addWidget(self._title)
        layout.addWidget(self._value)
        layout.addWidget(self._caption)

    def set_value(self, value: str, caption: str = "") -> None:
        self._value.setText(value)
        if caption:
            self._caption.setText(caption)
            self._caption.setVisible(True)

    def set_caption(self, caption: str) -> None:
        self._caption.setText(caption)
        self._caption.setVisible(bool(caption))
