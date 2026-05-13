"""Reps view — list of sales reps loaded from SALESMAN."""

from __future__ import annotations

import pandas as pd
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.config.models import DatabaseConfig
from app.data.loaders import load_reps
from app.ui.theme import TEXT_MUTED
from app.ui.views._header import ViewHeader


class _RepsLoader(QThread):
    loaded = Signal(object)  # pd.DataFrame
    failed = Signal(str)

    def __init__(self, db: DatabaseConfig) -> None:
        super().__init__()
        self._db = db

    def run(self) -> None:  # noqa: D401
        try:
            df = load_reps(self._db)
            self.loaded.emit(df)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class _PandasModel(QAbstractTableModel):
    def __init__(self, df: pd.DataFrame | None = None) -> None:
        super().__init__()
        self._df = df if df is not None else pd.DataFrame()

    def set_dataframe(self, df: pd.DataFrame) -> None:
        self.beginResetModel()
        self._df = df
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._df)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._df.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            label = str(self._df.columns[section])
            return label.replace("_", " ").title()
        return section + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        value = self._df.iat[index.row(), index.column()]
        if pd.isna(value):
            return ""
        return str(value)


class RepsView(QWidget):
    def __init__(self, get_db: callable, parent=None) -> None:
        super().__init__(parent)
        self._get_db = get_db
        self._loader: _RepsLoader | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)

        root.addWidget(
            ViewHeader(
                "Sales Reps",
                "Roster pulled from SALESMAN. Assignments and performance details land in the next iteration.",
            )
        )

        controls = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh from database")
        self.refresh_btn.setProperty("primary", True)
        self.refresh_btn.clicked.connect(self.reload)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by name or number…")
        self.search.textChanged.connect(self._apply_filter)
        controls.addWidget(self.refresh_btn)
        controls.addWidget(self.search, 1)
        root.addLayout(controls)

        self.status = QLabel("Click 'Refresh from database' to load reps.")
        self.status.setStyleSheet(f"color: {TEXT_MUTED};")
        root.addWidget(self.status)

        self.table = QTableView()
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._model = _PandasModel()
        self.table.setModel(self._model)
        root.addWidget(self.table, 1)

        self._all_df: pd.DataFrame | None = None

        # Auto-load on first show — never present an empty screen.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self.reload)

    def reload(self) -> None:
        self.refresh_btn.setEnabled(False)
        self.status.setText("Loading reps from database…")
        self._loader = _RepsLoader(self._get_db())
        self._loader.loaded.connect(self._on_loaded)
        self._loader.failed.connect(self._on_failed)
        self._loader.start()

    def _on_loaded(self, df: pd.DataFrame) -> None:
        self._all_df = df
        self._model.set_dataframe(df)
        self.status.setText(f"Loaded {len(df):,} rep(s).")
        self.refresh_btn.setEnabled(True)

    def _on_failed(self, msg: str) -> None:
        self.status.setText(f"Failed to load reps — {msg}")
        self.refresh_btn.setEnabled(True)

    def _apply_filter(self, text: str) -> None:
        if self._all_df is None:
            return
        if not text.strip():
            self._model.set_dataframe(self._all_df)
            return
        needle = text.strip().lower()
        mask = self._all_df.apply(
            lambda row: needle in " ".join(str(v).lower() for v in row.values),
            axis=1,
        )
        self._model.set_dataframe(self._all_df[mask].reset_index(drop=True))
