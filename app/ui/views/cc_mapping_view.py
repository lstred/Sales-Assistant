"""Cost-Center Mapping editor.

The original convention was "sample CCs start with ``1`` and map to product
CCs that start with ``0``", but in the live ``NRF_REPORTS`` warehouse no
codes start with ``1``. To keep this screen useful, we let the manager pick
**any** cost center and assign it to a parent (product) cost center — the
mapping persists in :class:`AppConfig.sample_to_product_cc`.

A **Show only unmapped** filter keeps the editor focused; a search box keeps
it scannable; auto-loads on first show; clear empty-state guidance when no
data is available.
"""

from __future__ import annotations

from typing import Callable

import pandas as pd
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.config.models import AppConfig, DatabaseConfig
from app.config.store import save_config
from app.data.loaders import load_all_cost_centers
from app.ui.theme import TEXT_MUTED
from app.ui.views._header import ViewHeader


class _CCLoader(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, db: DatabaseConfig) -> None:
        super().__init__()
        self._db = db

    def run(self) -> None:
        try:
            self.loaded.emit(load_all_cost_centers(self._db))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class CCMappingView(QWidget):
    def __init__(self, cfg: AppConfig, get_db: Callable[[], DatabaseConfig], parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._get_db = get_db
        self._loaders: list[_CCLoader] = []
        self._df: pd.DataFrame | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)

        root.addWidget(
            ViewHeader(
                "Cost Center Mapping",
                "Assign any cost center to a parent product cost center. "
                "Used to roll up sample / sub-line spending back to the "
                "product line that sponsors it.",
            )
        )

        controls = QHBoxLayout()
        self.refresh_btn = QPushButton("Reload from database")
        self.refresh_btn.setProperty("primary", True)
        self.refresh_btn.clicked.connect(self._reload)
        self.save_btn = QPushButton("Save mapping")
        self.save_btn.clicked.connect(self._save)
        controls.addWidget(self.refresh_btn)
        controls.addWidget(self.save_btn)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by code or name…")
        self.search.textChanged.connect(self._populate)
        controls.addWidget(self.search, 1)

        self.unmapped_only = QCheckBox("Show only unmapped")
        self.unmapped_only.toggled.connect(self._populate)
        controls.addWidget(self.unmapped_only)

        self.status = QLabel("Loading…")
        self.status.setStyleSheet(f"color: {TEXT_MUTED};")
        controls.addWidget(self.status)
        root.addLayout(controls)

        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(
            ["Cost Center", "Name", "Maps to (Parent CC)", "Parent Name"]
        )
        self.table.verticalHeader().setVisible(False)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)

        # Empty-state card
        self.empty_state = QFrame()
        self.empty_state.setObjectName("card")
        es = QVBoxLayout(self.empty_state)
        es.setContentsMargins(28, 28, 28, 28)
        es.setSpacing(10)
        title = QLabel("No cost centers loaded")
        title.setStyleSheet("font-size: 16px; font-weight: 600;")
        body = QLabel(
            "Cost centers come from <code>dbo.ITEM.[ICCTR]</code> in "
            "<i>NRF_REPORTS</i> (master list — includes sample <code>1xx</code> "
            "codes), with friendly names from <code>vw_CostCenterCLydeMRKCodeXREF</code>. "
            "Press <b>Reload from database</b> to populate this list. If your "
            "database connection isn't configured yet, set it up in "
            "<b>Settings → Database</b>."
        )
        body.setWordWrap(True)
        body.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 13px;")
        es.addWidget(title)
        es.addWidget(body)
        es.addStretch(1)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.empty_state)
        self.stack.addWidget(self.table)
        root.addWidget(self.stack, 1)

        QTimer.singleShot(0, self._reload)

    # --------------------------------------------------------------- actions
    def _reload(self) -> None:
        self.refresh_btn.setEnabled(False)
        self.status.setText("Loading cost centers…")
        loader = _CCLoader(self._get_db())
        loader.loaded.connect(self._on_loaded)
        loader.failed.connect(self._on_failed)
        self._loaders.append(loader)
        loader.finished.connect(
            lambda L=loader: self._loaders.remove(L) if L in self._loaders else None
        )
        loader.start()

    def _on_loaded(self, df: pd.DataFrame) -> None:
        df = (
            df.assign(cost_center=lambda d: d["cost_center"].astype(str).str.strip())
              .sort_values("cost_center")
              .drop_duplicates(subset=["cost_center"])
              .reset_index(drop=True)
        )
        self._df = df
        self.refresh_btn.setEnabled(True)
        self._populate()

    def _on_failed(self, msg: str) -> None:
        self.refresh_btn.setEnabled(True)
        self.status.setText(f"Failed — {msg}")
        self.stack.setCurrentIndex(0)

    def _populate(self) -> None:
        if self._df is None or self._df.empty:
            self.stack.setCurrentIndex(0)
            self.status.setText("No cost centers.")
            return

        self.stack.setCurrentIndex(1)
        df = self._df
        codes = df["cost_center"].tolist()
        names_by_code = dict(zip(df["cost_center"], df["cost_center_name"].fillna("")))

        needle = self.search.text().strip().lower()
        unmapped_only = self.unmapped_only.isChecked()
        mapping = self._cfg.sample_to_product_cc

        rows: list[tuple[str, str]] = []
        for code in codes:
            name = names_by_code.get(code, "")
            mapped = mapping.get(code, "")
            if unmapped_only and mapped:
                continue
            hay = f"{code} {name}".lower()
            if needle and needle not in hay:
                continue
            rows.append((code, name))

        self.table.setRowCount(len(rows))
        for r, (code, name) in enumerate(rows):
            self.table.setItem(r, 0, _ro(code))
            self.table.setItem(r, 1, _ro(name))
            cb = QComboBox()
            cb.addItem("(unassigned)", "")
            for pc in codes:
                if pc == code:
                    continue
                pc_name = names_by_code.get(pc, "")
                cb.addItem(f"{pc} — {pc_name}" if pc_name else pc, pc)
            current = mapping.get(code, "")
            idx = cb.findData(current)
            cb.setCurrentIndex(idx if idx >= 0 else 0)
            cb.currentIndexChanged.connect(
                lambda _i, row=r: self._update_parent_name(row)
            )
            self.table.setCellWidget(r, 2, cb)
            self.table.setItem(r, 3, _ro(names_by_code.get(current, "")))

        mapped_count = sum(1 for c in codes if mapping.get(c))
        self.status.setText(
            f"{len(codes)} cost center(s) · {mapped_count} mapped · "
            f"showing {len(rows)}"
        )

    def _update_parent_name(self, row: int) -> None:
        cb = self.table.cellWidget(row, 2)
        if cb is None or self._df is None:
            return
        target = (cb.currentData() or "").strip()
        names_by_code = dict(zip(
            self._df["cost_center"].astype(str), self._df["cost_center_name"].fillna("")
        ))
        self.table.setItem(row, 3, _ro(names_by_code.get(target, "")))

    def _save(self) -> None:
        if self._df is None:
            return
        mapping = dict(self._cfg.sample_to_product_cc)
        for r in range(self.table.rowCount()):
            code_item = self.table.item(r, 0)
            cb = self.table.cellWidget(r, 2)
            if not code_item or cb is None:
                continue
            code = code_item.text().strip()
            target = (cb.currentData() or "").strip()
            if target:
                mapping[code] = target
            else:
                mapping.pop(code, None)
        self._cfg.sample_to_product_cc = mapping
        save_config(self._cfg)
        self.status.setText(f"Saved · {len(mapping)} mapping(s) total.")


def _ro(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item
