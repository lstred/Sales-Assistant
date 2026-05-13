"""Reusable cost-center multi-select widget.

Loads CCs once from the warehouse, presents them in a checkable list with
*Select all*, *Clear*, and *Products only / Samples only* shortcuts, plus a
filter box. Emits :py:attr:`selection_changed` with the current list of
selected codes.

Conventions:
* Product cost centers start with ``'0'``.
* Sample cost centers start with ``'1'``.
"""

from __future__ import annotations

from typing import Callable

import pandas as pd
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.config.models import DatabaseConfig
from app.data.loaders import load_cost_centers
from app.ui.theme import TEXT_MUTED


class _CCLoader(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, db: DatabaseConfig) -> None:
        super().__init__()
        self._db = db

    def run(self) -> None:  # noqa: D401
        try:
            self.loaded.emit(load_cost_centers(self._db))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class CostCenterSelector(QWidget):
    selection_changed = Signal(list)  # list[str] of selected CC codes
    loaded = Signal(int)               # number of CCs loaded

    def __init__(self, get_db: Callable[[], DatabaseConfig], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._get_db = get_db
        self._loader: _CCLoader | None = None
        self._df: pd.DataFrame | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        title_row = QHBoxLayout()
        title = QLabel("Cost Centers")
        title.setStyleSheet("font-weight: 600;")
        self.count_label = QLabel("not loaded")
        self.count_label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(self.count_label)
        root.addLayout(title_row)

        # Action row
        actions = QHBoxLayout()
        self.refresh_btn = QPushButton("Reload")
        self.all_btn = QPushButton("All")
        self.none_btn = QPushButton("None")
        self.products_btn = QPushButton("Products only")
        self.samples_btn = QPushButton("Samples only")
        for b in (self.refresh_btn, self.all_btn, self.none_btn,
                  self.products_btn, self.samples_btn):
            actions.addWidget(b)
        actions.addStretch(1)
        root.addLayout(actions)

        self.refresh_btn.clicked.connect(self.reload)
        self.all_btn.clicked.connect(lambda: self._set_all(True, None))
        self.none_btn.clicked.connect(lambda: self._set_all(False, None))
        self.products_btn.clicked.connect(lambda: self._set_all(True, "0"))
        self.samples_btn.clicked.connect(lambda: self._set_all(True, "1"))

        # Filter
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter cost centers…")
        self.search.textChanged.connect(self._apply_filter)
        root.addWidget(self.search)

        # List
        self.model = QStandardItemModel(self)
        self.view = QListView()
        self.view.setModel(self.model)
        self.view.setSelectionMode(QListView.SelectionMode.NoSelection)
        self.view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        root.addWidget(self.view, 1)

        self.model.itemChanged.connect(self._on_item_changed)

    # --------------------------------------------------------------- public
    def selected_codes(self) -> list[str]:
        return [
            self.model.item(r).data(Qt.ItemDataRole.UserRole)
            for r in range(self.model.rowCount())
            if self.model.item(r).checkState() == Qt.CheckState.Checked
        ]

    def reload(self) -> None:
        self.refresh_btn.setEnabled(False)
        self.count_label.setText("loading…")
        self._loader = _CCLoader(self._get_db())
        self._loader.loaded.connect(self._on_loaded)
        self._loader.failed.connect(self._on_failed)
        self._loader.start()

    # --------------------------------------------------------------- internal
    def _on_loaded(self, df: pd.DataFrame) -> None:
        self._df = df
        self.refresh_btn.setEnabled(True)
        self.model.clear()
        for _, row in df.iterrows():
            code = str(row.get("cost_center", "")).strip()
            name = str(row.get("cost_center_name", "")).strip()
            label = f"{code}  —  {name}" if name else code
            it = QStandardItem(label)
            it.setData(code, Qt.ItemDataRole.UserRole)
            it.setCheckable(True)
            it.setCheckState(Qt.CheckState.Unchecked)
            it.setEditable(False)
            self.model.appendRow(it)
        self.count_label.setText(f"{len(df):,} loaded")
        self.loaded.emit(len(df))
        self.selection_changed.emit(self.selected_codes())

    def _on_failed(self, msg: str) -> None:
        self.refresh_btn.setEnabled(True)
        self.count_label.setText(f"failed: {msg}")

    def _on_item_changed(self, _item: QStandardItem) -> None:
        self.selection_changed.emit(self.selected_codes())

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for r in range(self.model.rowCount()):
            it = self.model.item(r)
            visible = (not needle) or needle in it.text().lower()
            self.view.setRowHidden(r, not visible)

    def _set_all(self, checked: bool, prefix: str | None) -> None:
        self.model.blockSignals(True)
        for r in range(self.model.rowCount()):
            it = self.model.item(r)
            code = it.data(Qt.ItemDataRole.UserRole) or ""
            if prefix is None or str(code).startswith(prefix):
                it.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            elif prefix is not None:
                it.setCheckState(Qt.CheckState.Unchecked)
        self.model.blockSignals(False)
        self.selection_changed.emit(self.selected_codes())
