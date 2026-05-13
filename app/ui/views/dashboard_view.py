"""Dashboard (placeholder summary cards until metrics land)."""

from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QVBoxLayout, QWidget

from app.ui.views._header import ViewHeader
from app.ui.widgets.cards import KpiCard


class DashboardView(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(16)

        root.addWidget(
            ViewHeader(
                "Overview",
                "Cross-territory snapshot of rep activity, conversations, and outstanding follow-ups.",
            )
        )

        grid = QGridLayout()
        grid.setSpacing(16)

        self.cards = {
            "active_reps": KpiCard("Active reps", "—"),
            "active_convos": KpiCard("Active conversations", "—"),
            "open_actions": KpiCard("Open action items", "—"),
            "needs_review": KpiCard("Needs review", "—", "Drafts awaiting your approval"),
        }
        positions = [(0, 0), (0, 1), (0, 2), (0, 3)]
        for (row, col), card in zip(positions, self.cards.values(), strict=True):
            grid.addWidget(card, row, col)

        root.addLayout(grid)
        root.addStretch(1)
