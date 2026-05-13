"""Reusable read-only QAbstractTableModel wrapper around a pandas DataFrame."""

from __future__ import annotations

import pandas as pd
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt


class PandasModel(QAbstractTableModel):
    NUMERIC_KINDS = ("i", "u", "f")

    def __init__(self, df: pd.DataFrame | None = None) -> None:
        super().__init__()
        self._df = df if df is not None else pd.DataFrame()

    # -------------------------------------------------------- model API
    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._df)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._df.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return str(self._df.columns[section]).replace("_", " ").title()
        return section + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        value = self._df.iat[index.row(), index.column()]
        col_dtype = self._df.dtypes.iloc[index.column()].kind
        if role == Qt.ItemDataRole.DisplayRole:
            if pd.isna(value):
                return ""
            if col_dtype in self.NUMERIC_KINDS:
                if isinstance(value, float):
                    if abs(value) >= 1000:
                        return f"{value:,.0f}"
                    return f"{value:,.2f}"
                return f"{int(value):,}"
            return str(value)
        if role == Qt.ItemDataRole.TextAlignmentRole and col_dtype in self.NUMERIC_KINDS:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    # -------------------------------------------------------- helpers
    def set_dataframe(self, df: pd.DataFrame) -> None:
        self.beginResetModel()
        self._df = df
        self.endResetModel()

    def dataframe(self) -> pd.DataFrame:
        return self._df
