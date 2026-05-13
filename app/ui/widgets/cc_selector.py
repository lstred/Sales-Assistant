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
from PySide6.QtCore import Qt, QThread, QTimer, Signal
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
from app.data.loaders import load_all_cost_centers
from app.ui.theme import TEXT_MUTED


class _CCLoader(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, db: DatabaseConfig) -> None:
        super().__init__()
        self._db = db

    def run(self) -> None:  # noqa: D401
        try:
            self.loaded.emit(load_all_cost_centers(self._db))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class CostCenterSelector(QWidget):
    selection_changed = Signal(list)  # list[str] of selected CC codes
    loaded = Signal(int)               # number of CCs loaded

    def __init__(
        self,
        get_db: Callable[[], DatabaseConfig],
        parent: QWidget | None = None,
        *,
        autoload: bool = True,
        select_all_after_load: bool = True,
        code_prefix_filter: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._get_db = get_db
        self._loader: _CCLoader | None = None
        self._df: pd.DataFrame | None = None
        self._select_all_after_load = select_all_after_load
        self._prefix_filter = (code_prefix_filter or "").strip()

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

        # Two minimalist actions: Select all / Deselect all. Manager picks
        # individual codes by ticking rows directly.
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self.all_btn = QPushButton("Select all")
        self.none_btn = QPushButton("Deselect all")
        for b in (self.all_btn, self.none_btn):
            btn_row.addWidget(b, 1)
        root.addLayout(btn_row)

        self.all_btn.clicked.connect(lambda: self._set_all(True))
        self.none_btn.clicked.connect(lambda: self._set_all(False))

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
        if autoload:
            QTimer.singleShot(0, self.reload)

    # --------------------------------------------------------------- public
    def selected_codes(self) -> list[str]:
        return [
            self.model.item(r).data(Qt.ItemDataRole.UserRole)
            for r in range(self.model.rowCount())
            if self.model.item(r).checkState() == Qt.CheckState.Checked
        ]

    def reload(self) -> None:
        self.count_label.setText("loading…")
        self._loader = _CCLoader(self._get_db())
        self._loader.loaded.connect(self._on_loaded)
        self._loader.failed.connect(self._on_failed)
        self._loader.start()

    # --------------------------------------------------------------- internal
    def _on_loaded(self, df: pd.DataFrame) -> None:
        if self._prefix_filter and df is not None and not df.empty:
            df = df[df["cost_center"].astype(str).str.startswith(self._prefix_filter)]
        self._df = df
        self.model.blockSignals(True)
        self.model.clear()
        for _, row in df.iterrows():
            code = str(row.get("cost_center", "")).strip()
            name = str(row.get("cost_center_name", "")).strip()
            label = f"{code}  —  {name}" if name else code
            it = QStandardItem(label)
            it.setData(code, Qt.ItemDataRole.UserRole)
            it.setCheckable(True)
            initially_checked = self._select_all_after_load
            it.setCheckState(
                Qt.CheckState.Checked if initially_checked else Qt.CheckState.Unchecked
            )
            it.setEditable(False)
            self.model.appendRow(it)
        self.model.blockSignals(False)
        self.count_label.setText(
            f"{len(df):,} loaded · "
            f"{sum(1 for r in range(self.model.rowCount()) if self.model.item(r).checkState() == Qt.CheckState.Checked):,} selected"
        )
        self.loaded.emit(len(df))
        self.selection_changed.emit(self.selected_codes())

    def _on_failed(self, msg: str) -> None:
        self.count_label.setText(f"failed: {msg}")

    def _on_item_changed(self, _item: QStandardItem) -> None:
        self._refresh_count()
        self.selection_changed.emit(self.selected_codes())

    def _refresh_count(self) -> None:
        if self._df is None:
            return
        sel = sum(
            1 for r in range(self.model.rowCount())
            if self.model.item(r).checkState() == Qt.CheckState.Checked
        )
        self.count_label.setText(f"{len(self._df):,} loaded · {sel:,} selected")

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for r in range(self.model.rowCount()):
            it = self.model.item(r)
            visible = (not needle) or needle in it.text().lower()
            self.view.setRowHidden(r, not visible)

    def _set_all(self, checked: bool) -> None:
        self.model.blockSignals(True)
        for r in range(self.model.rowCount()):
            it = self.model.item(r)
            it.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.model.blockSignals(False)
        self._refresh_count()
        self.selection_changed.emit(self.selected_codes())
