"""Reps view — list of sales reps loaded from SALESMAN.

The screen also doubles as the **Rep Directory**: email address, escalation
CC, and tone bias are editable inline and persist to
:class:`~app.config.models.AppConfig` (which the Weekly Email + outbound
flows then read).
"""

from __future__ import annotations

import pandas as pd
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.config.models import AppConfig, DatabaseConfig
from app.config.store import save_config
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


EDITABLE_COLS = {"email", "boss_email", "tone"}


class _RepsModel(QAbstractTableModel):
    """Editable model. Edits to ``email``/``boss_email``/``tone`` columns are
    written through to :class:`AppConfig` via the ``on_edit`` callback."""

    cell_changed = Signal(str, str, str)  # salesman_number, column, new_value

    def __init__(self, df: pd.DataFrame | None = None) -> None:
        super().__init__()
        self._df = df if df is not None else pd.DataFrame()

    def set_dataframe(self, df: pd.DataFrame) -> None:
        self.beginResetModel()
        self._df = df.reset_index(drop=True)
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
        if not index.isValid():
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            value = self._df.iat[index.row(), index.column()]
            if pd.isna(value):
                return ""
            return str(value)
        return None

    def flags(self, index):
        base = super().flags(index)
        if not index.isValid():
            return base
        col = str(self._df.columns[index.column()])
        if col in EDITABLE_COLS:
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        col = str(self._df.columns[index.column()])
        if col not in EDITABLE_COLS:
            return False
        self._df.iat[index.row(), index.column()] = str(value or "").strip()
        slmn = str(self._df.iat[index.row(), self._df.columns.get_loc("salesman_number")])
        self.cell_changed.emit(slmn, col, str(value or "").strip())
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.DisplayRole])
        return True


class RepsView(QWidget):
    def __init__(self, cfg: AppConfig, get_db: callable, parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._get_db = get_db
        self._loaders: list[_RepsLoader] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)

        root.addWidget(
            ViewHeader(
                "Sales Reps & Directory",
                "Roster pulled from SALESMAN. Email, escalation CC, and tone bias "
                "are editable inline — changes save automatically and feed the "
                "Weekly Email composer.",
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

        self.status = QLabel("Loading reps…")
        self.status.setStyleSheet(f"color: {TEXT_MUTED};")
        root.addWidget(self.status)

        self.table = QTableView()
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._model = _RepsModel()
        self._model.cell_changed.connect(self._on_cell_changed)
        self.table.setModel(self._model)
        root.addWidget(self.table, 1)

        self._all_df: pd.DataFrame | None = None

        # Auto-load on first show — never present an empty screen.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self.reload)

    def reload(self) -> None:
        self.refresh_btn.setEnabled(False)
        self.status.setText("Loading reps from database…")
        loader = _RepsLoader(self._get_db())
        loader.loaded.connect(self._on_loaded)
        loader.failed.connect(self._on_failed)
        self._loaders.append(loader)
        loader.finished.connect(
            lambda L=loader: self._loaders.remove(L) if L in self._loaders else None
        )
        loader.start()

    def _on_loaded(self, df: pd.DataFrame) -> None:
        # Augment with editable directory columns sourced from cfg.
        df = df.copy()
        df["email"] = df["salesman_number"].map(
            lambda n: self._cfg.rep_emails.get(str(n).strip(), "")
        )
        df["boss_email"] = df["salesman_number"].map(
            lambda n: self._cfg.rep_boss_emails.get(str(n).strip(), "")
        )
        df["tone"] = df["salesman_number"].map(
            lambda n: str(self._cfg.rep_tone.get(str(n).strip(), ""))
        )
        self._all_df = df
        self._model.set_dataframe(df)
        n_emails = sum(1 for v in df["email"] if v)
        self.status.setText(
            f"Loaded {len(df):,} rep(s). {n_emails:,} have an email on file. "
            "Click any cell in Email / Boss Email / Tone to edit."
        )
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

    def _on_cell_changed(self, salesman_number: str, column: str, value: str) -> None:
        slmn = str(salesman_number or "").strip()
        if not slmn:
            return
        v = (value or "").strip()
        try:
            if column == "email":
                if v:
                    self._cfg.rep_emails[slmn] = v
                else:
                    self._cfg.rep_emails.pop(slmn, None)
            elif column == "boss_email":
                if v:
                    self._cfg.rep_boss_emails[slmn] = v
                else:
                    self._cfg.rep_boss_emails.pop(slmn, None)
            elif column == "tone":
                if v:
                    try:
                        self._cfg.rep_tone[slmn] = max(-3, min(3, int(v)))
                    except ValueError:
                        # Non-integer tone is rejected silently — the user sees
                        # their typo persist in the cell so they can fix it.
                        return
                else:
                    self._cfg.rep_tone.pop(slmn, None)
            save_config(self._cfg)
            self.status.setText(f"Saved {column} for #{slmn}.")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Save failed", f"{type(exc).__name__}: {exc}")
