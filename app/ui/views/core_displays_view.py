"""Assign 'core' display codes to cost centers.

Each row is one display (``CLASSES.CLCODE`` where ``CLCAT='DT'``). The
manager picks one or more cost centers that consider this display "core"
to their product line. The mapping persists in
:attr:`AppConfig.core_displays_by_cc` (keyed by CC -> list[display_code]),
so insights can later surface things like "Account X dropped its core
display Y after install date Z".
"""

from __future__ import annotations

from typing import Callable

import pandas as pd
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.config.models import AppConfig, DatabaseConfig
from app.config.store import save_config
from app.data.loaders import load_cost_centers, load_display_types
from app.ui.theme import TEXT_MUTED
from app.ui.views._header import ViewHeader


class _Loader(QThread):
    loaded = Signal(object, object)  # displays_df, cost_centers_df
    failed = Signal(str)

    def __init__(self, db: DatabaseConfig) -> None:
        super().__init__()
        self._db = db

    def run(self) -> None:
        try:
            disp = load_display_types(self._db)
            ccs = load_cost_centers(self._db)
            self.loaded.emit(disp, ccs)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class CoreDisplaysView(QWidget):
    def __init__(self, cfg: AppConfig, get_db: Callable[[], DatabaseConfig], parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._get_db = get_db
        self._loaders: list[_Loader] = []
        self._displays: pd.DataFrame | None = None
        self._ccs: pd.DataFrame | None = None
        self._cc_codes: list[str] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)

        root.addWidget(
            ViewHeader(
                "Core Displays per Cost Center",
                "Each display (CLASSES.CLCAT='DT') can be marked as 'core' "
                "for one or more cost centers. Used by insights to flag "
                "accounts that lost their core display.",
            )
        )

        controls = QHBoxLayout()
        self.refresh_btn = QPushButton("Reload from database")
        self.refresh_btn.setProperty("primary", True)
        self.refresh_btn.clicked.connect(self._reload)
        self.save_btn = QPushButton("Save assignments")
        self.save_btn.clicked.connect(self._save)
        controls.addWidget(self.refresh_btn)
        controls.addWidget(self.save_btn)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter displays by code or description…")
        self.search.textChanged.connect(self._populate)
        controls.addWidget(self.search, 1)

        self.status = QLabel("Loading…")
        self.status.setStyleSheet(f"color: {TEXT_MUTED};")
        controls.addWidget(self.status)
        root.addLayout(controls)

        body = QHBoxLayout()
        body.setSpacing(12)

        # ----- left: displays table
        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["Display Code", "Description", "Assigned CCs"])
        self.table.verticalHeader().setVisible(False)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.itemSelectionChanged.connect(self._on_row_changed)

        # ----- right: cost-center checklist for selected display
        right = QFrame()
        right.setObjectName("card")
        right.setMinimumWidth(280)
        right.setMaximumWidth(360)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(14, 14, 14, 14)
        rv.setSpacing(8)
        self.right_title = QLabel("Pick a display →")
        self.right_title.setStyleSheet("font-weight: 600;")
        rv.addWidget(self.right_title)
        self.cc_search = QLineEdit()
        self.cc_search.setPlaceholderText("Filter cost centers…")
        self.cc_search.textChanged.connect(self._populate_cc_list)
        rv.addWidget(self.cc_search)
        self.cc_list = QListWidget()
        self.cc_list.itemChanged.connect(self._on_cc_toggled)
        rv.addWidget(self.cc_list, 1)
        hint = QLabel("Tick every cost center that considers this display "
                      "core to its product line.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        rv.addWidget(hint)

        # Empty state
        self.empty_state = QFrame()
        self.empty_state.setObjectName("card")
        es = QVBoxLayout(self.empty_state)
        es.setContentsMargins(28, 28, 28, 28)
        es_title = QLabel("No displays loaded")
        es_title.setStyleSheet("font-size: 16px; font-weight: 600;")
        es_body = QLabel(
            "Displays come from <code>dbo.CLASSES</code> with "
            "<code>CLCAT='DT'</code>. Press <b>Reload from database</b> "
            "to populate."
        )
        es_body.setWordWrap(True)
        es_body.setStyleSheet(f"color: {TEXT_MUTED};")
        es.addWidget(es_title)
        es.addWidget(es_body)
        es.addStretch(1)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.empty_state)
        self.stack.addWidget(self.table)
        body.addWidget(self.stack, 1)
        body.addWidget(right)
        root.addLayout(body, 1)

        QTimer.singleShot(0, self._reload)

    # --------------------------------------------------------------- actions
    def _reload(self) -> None:
        self.refresh_btn.setEnabled(False)
        self.status.setText("Loading displays + cost centers…")
        loader = _Loader(self._get_db())
        loader.loaded.connect(self._on_loaded)
        loader.failed.connect(self._on_failed)
        self._loaders.append(loader)
        loader.finished.connect(
            lambda L=loader: self._loaders.remove(L) if L in self._loaders else None
        )
        loader.start()

    def _on_loaded(self, displays: pd.DataFrame, ccs: pd.DataFrame) -> None:
        self.refresh_btn.setEnabled(True)
        self._displays = (
            displays.assign(display_code=lambda d: d["display_code"].astype(str).str.strip())
                    .drop_duplicates(subset=["display_code"])
                    .sort_values("display_code")
                    .reset_index(drop=True)
        )
        self._ccs = (
            ccs.assign(cost_center=lambda d: d["cost_center"].astype(str).str.strip())
               .drop_duplicates(subset=["cost_center"])
               .sort_values("cost_center")
               .reset_index(drop=True)
        )
        self._cc_codes = self._ccs["cost_center"].tolist()
        self._populate()
        self._populate_cc_list()

    def _on_failed(self, msg: str) -> None:
        self.refresh_btn.setEnabled(True)
        self.status.setText(f"Failed — {msg}")
        self.stack.setCurrentIndex(0)

    # --------------------------------------------------------------- table
    def _populate(self) -> None:
        if self._displays is None or self._displays.empty:
            self.stack.setCurrentIndex(0)
            self.status.setText("No displays.")
            return
        self.stack.setCurrentIndex(1)

        needle = self.search.text().strip().lower()
        # Reverse map: display_code -> set of CC codes that include it as core
        display_to_ccs: dict[str, list[str]] = {}
        for cc, codes in (self._cfg.core_displays_by_cc or {}).items():
            for dc in codes or []:
                display_to_ccs.setdefault(str(dc).strip(), []).append(str(cc).strip())

        rows = []
        for _, row in self._displays.iterrows():
            code = row["display_code"]
            desc = row.get("display_desc", "") or ""
            hay = f"{code} {desc}".lower()
            if needle and needle not in hay:
                continue
            rows.append((code, desc, display_to_ccs.get(code, [])))

        self.table.setRowCount(len(rows))
        for r, (code, desc, ccs_for_disp) in enumerate(rows):
            self.table.setItem(r, 0, _ro(code))
            self.table.setItem(r, 1, _ro(desc))
            self.table.setItem(r, 2, _ro(", ".join(sorted(set(ccs_for_disp))) or "—"))

        self.status.setText(
            f"{len(self._displays)} display(s) · {sum(len(v) for v in display_to_ccs.values())} "
            f"assignment(s) · showing {len(rows)}"
        )

    def _selected_display_code(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.text() if item else None

    def _on_row_changed(self) -> None:
        code = self._selected_display_code()
        if not code:
            self.right_title.setText("Pick a display →")
            return
        desc_item = self.table.item(self.table.currentRow(), 1)
        self.right_title.setText(f"{code} — {desc_item.text() if desc_item else ''}")
        self._populate_cc_list()

    def _populate_cc_list(self) -> None:
        self.cc_list.blockSignals(True)
        self.cc_list.clear()
        sel_code = self._selected_display_code()
        if not sel_code or self._ccs is None:
            self.cc_list.blockSignals(False)
            return
        needle = self.cc_search.text().strip().lower()
        names_by_code = dict(zip(
            self._ccs["cost_center"], self._ccs["cost_center_name"].fillna("")
        ))
        for cc in self._cc_codes:
            label = f"{cc} — {names_by_code.get(cc, '')}".rstrip(" —")
            if needle and needle not in label.lower():
                continue
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, cc)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            assigned = sel_code in (self._cfg.core_displays_by_cc.get(cc) or [])
            it.setCheckState(Qt.CheckState.Checked if assigned else Qt.CheckState.Unchecked)
            self.cc_list.addItem(it)
        self.cc_list.blockSignals(False)

    def _on_cc_toggled(self, item: QListWidgetItem) -> None:
        sel_code = self._selected_display_code()
        if not sel_code:
            return
        cc = item.data(Qt.ItemDataRole.UserRole)
        current = list(self._cfg.core_displays_by_cc.get(cc) or [])
        if item.checkState() == Qt.CheckState.Checked:
            if sel_code not in current:
                current.append(sel_code)
        else:
            current = [c for c in current if c != sel_code]
        new_map = dict(self._cfg.core_displays_by_cc)
        if current:
            new_map[cc] = current
        else:
            new_map.pop(cc, None)
        self._cfg.core_displays_by_cc = new_map
        # Update count column for the selected row
        row = self.table.currentRow()
        if row >= 0:
            assigned_ccs = [
                ccode for ccode, codes in new_map.items() if sel_code in (codes or [])
            ]
            self.table.setItem(row, 2, _ro(", ".join(sorted(assigned_ccs)) or "—"))

    def _save(self) -> None:
        save_config(self._cfg)
        total = sum(len(v or []) for v in self._cfg.core_displays_by_cc.values())
        self.status.setText(f"Saved · {total} display→CC assignment(s).")


def _ro(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item
