"""Sidebar with brand and exclusive nav buttons."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QVBoxLayout,
)


class Sidebar(QFrame):
    navigated = Signal(str)  # emits the destination view key

    def __init__(self, items: list[tuple[str, str]], parent=None) -> None:
        """``items`` is a list of (key, label) tuples in display order."""
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setFixedWidth(220)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        brand = QLabel("Sales Assistant")
        brand.setObjectName("sidebarBrand")
        tagline = QLabel("AI-powered sales coaching")
        tagline.setObjectName("sidebarTagline")
        layout.addWidget(brand)
        layout.addWidget(tagline)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QPushButton] = {}

        for key, label in items:
            btn = QPushButton(label)
            btn.setObjectName("navButton")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, k=key: self.navigated.emit(k))
            self._group.addButton(btn)
            layout.addWidget(btn)
            self._buttons[key] = btn

        layout.addItem(QSpacerItem(0, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding))

        version = QLabel("v0.1.0")
        version.setObjectName("sidebarTagline")
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(version)

    def select(self, key: str) -> None:
        if key in self._buttons:
            self._buttons[key].setChecked(True)
