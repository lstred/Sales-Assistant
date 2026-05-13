"""Sample-to-Product cost-center mapping editor.

Sample CCs (codes starting with ``'1'``) are mapped to product CCs (codes
starting with ``'0'``) so that sample expenses can be attributed back to
their sponsoring product line.
"""

from __future__ import annotations

from typing import Callable

import pandas as pd
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.config.models import AppConfig, DatabaseConfig
from app.config.store import save_config
from app.data.loaders import load_cost_centers
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
            self.loaded.emit(load_cost_centers(self._db))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class CCMappingView(QWidget):
    def __init__(self, cfg: AppConfig, get_db: Callable[[], DatabaseConfig], parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._get_db = get_db
        self._loader: _CCLoader | None = None
        self._df: pd.DataFrame | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)

        root.addWidget(
            ViewHeader(
                "Sample → Product Cost Center Mapping",
                "Sample cost centers (codes starting with 1) are linked to their "
                "sponsoring product cost centers (codes starting with 0). "
                "Used to attribute sample expense back to the right product line.",
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
        controls.addStretch(1)
        self.status = QLabel("Press Reload to populate.")
        self.status.setStyleSheet(f"color: {TEXT_MUTED};")
        controls.addWidget(self.status)
        root.addLayout(controls)

        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["Sample CC", "Sample Name", "Maps to Product CC"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        root.addWidget(self.table, 1)

    # --------------------------------------------------------------- actions
    def _reload(self) -> None:
        self.refresh_btn.setEnabled(False)
        self.status.setText("Loading cost centers…")
        self._loader = _CCLoader(self._get_db())
        self._loader.loaded.connect(self._on_loaded)
        self._loader.failed.connect(self._on_failed)
        self._loader.start()

    def _on_loaded(self, df: pd.DataFrame) -> None:
        self._df = df
        self.refresh_btn.setEnabled(True)
        self._populate()

    def _on_failed(self, msg: str) -> None:
        self.refresh_btn.setEnabled(True)
        self.status.setText(f"Failed — {msg}")

    def _populate(self) -> None:
        if self._df is None:
            return
        codes = self._df["cost_center"].astype(str).str.strip()
        names = self._df["cost_center_name"].astype(str).fillna("").str.strip()

        sample_mask = codes.str.startswith("1")
        product_codes = sorted(c for c in codes[codes.str.startswith("0")].tolist() if c)

        sample_rows = self._df[sample_mask].sort_values("cost_center").reset_index(drop=True)
        self.table.setRowCount(len(sample_rows))
        for r, row in sample_rows.iterrows():
            code = str(row["cost_center"]).strip()
            name = str(row.get("cost_center_name", "")).strip()
            self.table.setItem(r, 0, _ro(code))
            self.table.setItem(r, 1, _ro(name))
            cb = QComboBox()
            cb.addItem("(unassigned)", "")
            for pc in product_codes:
                cb.addItem(pc, pc)
            current = self._cfg.sample_to_product_cc.get(code, "")
            idx = cb.findData(current)
            cb.setCurrentIndex(idx if idx >= 0 else 0)
            self.table.setCellWidget(r, 2, cb)
        self.status.setText(f"{len(sample_rows)} sample CCs · {len(product_codes)} product CCs available.")

    def _save(self) -> None:
        mapping: dict[str, str] = {}
        for r in range(self.table.rowCount()):
            code_item = self.table.item(r, 0)
            cb = self.table.cellWidget(r, 2)
            if not code_item or cb is None:
                continue
            code = code_item.text().strip()
            target = (cb.currentData() or "").strip()
            if code and target:
                mapping[code] = target
        self._cfg.sample_to_product_cc = mapping
        save_config(self._cfg)
        self.status.setText(f"Saved {len(mapping)} mapping(s).")


def _ro(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item
