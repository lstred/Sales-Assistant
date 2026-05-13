"""Header (title + subtitle) used at the top of every view."""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class ViewHeader(QWidget):
    def __init__(self, title: str, subtitle: str = "", parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 8)
        layout.setSpacing(2)
        t = QLabel(title)
        t.setObjectName("viewTitle")
        layout.addWidget(t)
        if subtitle:
            s = QLabel(subtitle)
            s.setObjectName("viewSubtitle")
            s.setWordWrap(True)
            layout.addWidget(s)
