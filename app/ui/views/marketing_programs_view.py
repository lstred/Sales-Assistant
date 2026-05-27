"""Marketing Programs view.

Lists every marketing program (``CLASSES.CLCODE`` where ``CLCAT='MP'``),
lets the manager:
  • tag each program with a high-level category from a user-editable list
    (defaults: 'CCA Buying Group', 'NRF Rebate Program');
  • star programs as 'important' so the AI surfaces them in every analysis;
  • inspect the accounts enrolled in the selected program.

All settings persist to :class:`AppConfig` via :func:`save_config`. The
data is consumed by every AI surface through
:mod:`app.services.marketing_programs`.
"""

from __future__ import annotations

from typing import Callable

import pandas as pd
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.config.models import AppConfig, DatabaseConfig
from app.config.store import save_config
from app.data.loaders import (
    load_marketing_program_placements,
    load_marketing_program_types,
)
from app.services.marketing_programs import UNCATEGORIZED
from app.ui.theme import TEXT_MUTED
from app.ui.views._header import ViewHeader


class _Loader(QThread):
    loaded = Signal(object, object)  # programs_df, placements_df
    failed = Signal(str)

    def __init__(self, db: DatabaseConfig) -> None:
        super().__init__()
        self._db = db

    def run(self) -> None:
        try:
            progs = load_marketing_program_types(self._db)
            place = load_marketing_program_placements(self._db)
            self.loaded.emit(progs, place)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


def _ro(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


class MarketingProgramsView(QWidget):
    def __init__(self, cfg: AppConfig, get_db: Callable[[], DatabaseConfig], parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._get_db = get_db
        self._loaders: list[_Loader] = []
        self._programs: pd.DataFrame | None = None
        self._placements: pd.DataFrame | None = None
        self._suspend_signals = False

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)

        root.addWidget(
            ViewHeader(
                "Marketing Programs",
                "Tag each program (CLASSES.CLCAT='MP') with a high-level "
                "category and star the ones that matter. The AI uses these "
                "tags everywhere — per-rep emails, conversations, Ask the AI "
                "— to look for correlations between program enrollment and "
                "rep / account performance.",
            )
        )

        controls = QHBoxLayout()
        self.refresh_btn = QPushButton("Reload from database")
        self.refresh_btn.setProperty("primary", True)
        self.refresh_btn.clicked.connect(self._reload)

        self.add_cat_btn = QPushButton("➕ New category…")
        self.add_cat_btn.setToolTip(
            "Add a custom high-level category (e.g. 'Co-op', 'Spiff'). "
            "Becomes selectable in the per-program dropdown."
        )
        self.add_cat_btn.clicked.connect(self._add_category)

        self.save_btn = QPushButton("Save")
        self.save_btn.setProperty("primary", True)
        self.save_btn.clicked.connect(self._save)

        controls.addWidget(self.refresh_btn)
        controls.addWidget(self.add_cat_btn)
        controls.addWidget(self.save_btn)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter programs by code or description…")
        self.search.textChanged.connect(self._populate_table)
        controls.addWidget(self.search, 1)

        self.status = QLabel("Loading…")
        self.status.setStyleSheet(f"color: {TEXT_MUTED};")
        controls.addWidget(self.status)
        root.addLayout(controls)

        body = QHBoxLayout()
        body.setSpacing(12)

        # ----- left: programs table
        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(
            ["⭐", "Program Code", "Description", "Category", "Enrolled Accounts"]
        )
        self.table.verticalHeader().setVisible(False)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.itemSelectionChanged.connect(self._on_row_changed)
        self.table.itemChanged.connect(self._on_item_changed)

        # ----- right: accounts enrolled in selected program
        right = QFrame()
        right.setObjectName("card")
        right.setMinimumWidth(300)
        right.setMaximumWidth(420)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(14, 14, 14, 14)
        rv.setSpacing(8)
        self.right_title = QLabel("Pick a program →")
        self.right_title.setStyleSheet("font-weight: 600;")
        rv.addWidget(self.right_title)
        self.accounts_search = QLineEdit()
        self.accounts_search.setPlaceholderText("Filter accounts…")
        self.accounts_search.textChanged.connect(self._populate_accounts)
        rv.addWidget(self.accounts_search)
        self.accounts_list = QListWidget()
        rv.addWidget(self.accounts_list, 1)
        self.accounts_count = QLabel("")
        self.accounts_count.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        rv.addWidget(self.accounts_count)

        # Empty state
        self.empty_state = QFrame()
        self.empty_state.setObjectName("card")
        es = QVBoxLayout(self.empty_state)
        es.setContentsMargins(28, 28, 28, 28)
        es_title = QLabel("No marketing programs loaded")
        es_title.setStyleSheet("font-size: 16px; font-weight: 600;")
        es_body = QLabel(
            "Programs come from <code>dbo.CLASSES</code> with "
            "<code>CLCAT='MP'</code>. Press <b>Reload from database</b> "
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

    # --------------------------------------------------------------- load
    def _reload(self) -> None:
        self.refresh_btn.setEnabled(False)
        self.status.setText("Loading marketing programs…")
        loader = _Loader(self._get_db())
        loader.loaded.connect(self._on_loaded)
        loader.failed.connect(self._on_failed)
        self._loaders.append(loader)
        loader.finished.connect(
            lambda L=loader: self._loaders.remove(L) if L in self._loaders else None
        )
        loader.start()

    def _on_loaded(self, programs: pd.DataFrame, placements: pd.DataFrame) -> None:
        self.refresh_btn.setEnabled(True)
        self._programs = (
            programs.assign(program_code=lambda d: d["program_code"].astype(str).str.strip())
            .drop_duplicates(subset=["program_code"])
            .sort_values("program_code")
            .reset_index(drop=True)
        )
        self._placements = (
            placements.assign(
                program_code=lambda d: d["program_code"].astype(str).str.strip(),
                account_number=lambda d: d["account_number"].astype(str).str.strip(),
            )
            if placements is not None and not placements.empty
            else pd.DataFrame(columns=["account_number", "program_code", "program_desc"])
        )
        self._populate_table()
        self._populate_accounts()

    def _on_failed(self, msg: str) -> None:
        self.refresh_btn.setEnabled(True)
        self.status.setText(f"Failed — {msg}")
        self.stack.setCurrentIndex(0)

    # --------------------------------------------------------------- table
    def _enrolled_counts(self) -> dict[str, int]:
        if self._placements is None or self._placements.empty:
            return {}
        return (
            self._placements.groupby("program_code")["account_number"]
            .nunique()
            .to_dict()
        )

    def _populate_table(self) -> None:
        if self._programs is None or self._programs.empty:
            self.stack.setCurrentIndex(0)
            self.status.setText("No programs.")
            return
        self.stack.setCurrentIndex(1)

        needle = self.search.text().strip().lower()
        counts = self._enrolled_counts()
        cat_by_code = self._cfg.marketing_program_category_by_code or {}
        starred = set(self._cfg.marketing_program_starred or [])

        rows = []
        for _, row in self._programs.iterrows():
            code = row["program_code"]
            desc = row.get("program_desc", "") or ""
            if needle and needle not in f"{code} {desc}".lower():
                continue
            rows.append((code, desc, cat_by_code.get(code, UNCATEGORIZED), counts.get(code, 0)))

        self._suspend_signals = True
        self.table.setRowCount(len(rows))
        for r, (code, desc, cat, n) in enumerate(rows):
            star_item = QTableWidgetItem("⭐" if code in starred else "")
            star_item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            star_item.setCheckState(
                Qt.CheckState.Checked if code in starred else Qt.CheckState.Unchecked
            )
            star_item.setData(Qt.ItemDataRole.UserRole, ("star", code))
            star_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(r, 0, star_item)

            code_item = _ro(code)
            code_item.setData(Qt.ItemDataRole.UserRole, code)
            self.table.setItem(r, 1, code_item)
            self.table.setItem(r, 2, _ro(desc))

            # Category combo widget per row.
            combo = QComboBox()
            combo.addItem(UNCATEGORIZED)
            for c in self._cfg.marketing_program_categories or []:
                if c and c != UNCATEGORIZED:
                    combo.addItem(c)
            # Set current category — add it on-the-fly if it's an orphan
            # (user removed it from the categories list but a code still
            # references it).
            if cat and cat != UNCATEGORIZED and combo.findText(cat) < 0:
                combo.addItem(cat)
            combo.setCurrentText(cat or UNCATEGORIZED)
            combo.currentTextChanged.connect(
                lambda new_cat, prog_code=code: self._set_category(prog_code, new_cat)
            )
            self.table.setCellWidget(r, 3, combo)
            self.table.setItem(r, 3, _ro(""))  # placeholder so row sizing is correct

            count_item = _ro(str(n))
            count_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(r, 4, count_item)
        self._suspend_signals = False

        total = len(self._programs)
        n_starred = sum(1 for r in rows if r[0] in starred)
        n_categorized = sum(1 for r in rows if r[2] != UNCATEGORIZED)
        self.status.setText(
            f"{total} program(s) · {n_categorized}/{len(rows)} categorized in view · "
            f"{n_starred} starred"
        )

    def _selected_program_code(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 1)
        return item.text() if item else None

    def _on_row_changed(self) -> None:
        code = self._selected_program_code()
        if not code:
            self.right_title.setText("Pick a program →")
            self.accounts_list.clear()
            self.accounts_count.setText("")
            return
        desc_item = self.table.item(self.table.currentRow(), 2)
        self.right_title.setText(f"{code} — {desc_item.text() if desc_item else ''}")
        self._populate_accounts()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._suspend_signals:
            return
        payload = item.data(Qt.ItemDataRole.UserRole)
        if not (isinstance(payload, tuple) and len(payload) == 2 and payload[0] == "star"):
            return
        code = payload[1]
        starred = set(self._cfg.marketing_program_starred or [])
        if item.checkState() == Qt.CheckState.Checked:
            starred.add(code)
            item.setText("⭐")
        else:
            starred.discard(code)
            item.setText("")
        self._cfg.marketing_program_starred = sorted(starred)

    def _set_category(self, program_code: str, new_cat: str) -> None:
        cat_map = dict(self._cfg.marketing_program_category_by_code or {})
        new_cat_clean = (new_cat or "").strip()
        if not new_cat_clean or new_cat_clean == UNCATEGORIZED:
            cat_map.pop(program_code, None)
        else:
            cat_map[program_code] = new_cat_clean
        self._cfg.marketing_program_category_by_code = cat_map

    def _add_category(self) -> None:
        name, ok = QInputDialog.getText(
            self, "New category", "Category name (e.g. 'Co-op', 'Spiff'):"
        )
        if not ok:
            return
        name = (name or "").strip()
        if not name or name == UNCATEGORIZED:
            return
        cats = list(self._cfg.marketing_program_categories or [])
        if name in cats:
            QMessageBox.information(
                self, "Already exists", f"Category '{name}' already exists."
            )
            return
        cats.append(name)
        self._cfg.marketing_program_categories = cats
        # Append the new option to every existing combo without losing
        # the current selections.
        for r in range(self.table.rowCount()):
            combo = self.table.cellWidget(r, 3)
            if isinstance(combo, QComboBox) and combo.findText(name) < 0:
                combo.addItem(name)
        self.status.setText(f"Added category '{name}' · don't forget to Save.")

    # --------------------------------------------------------------- accounts
    def _populate_accounts(self) -> None:
        self.accounts_list.clear()
        sel = self._selected_program_code()
        if not sel or self._placements is None or self._placements.empty:
            self.accounts_count.setText("")
            return
        sub = self._placements[self._placements["program_code"] == sel]
        needle = self.accounts_search.text().strip().lower()
        rows = []
        for acct, sub2 in sub.groupby("account_number"):
            on = sub2.get("enrolled_on")
            on_label = ""
            if on is not None and not on.empty:
                on_val = on.iloc[0]
                if pd.notna(on_val):
                    try:
                        on_label = pd.Timestamp(on_val).strftime("%Y-%m-%d")
                    except Exception:  # noqa: BLE001
                        on_label = ""
            label = f"{acct}" + (f"  ·  enrolled {on_label}" if on_label else "")
            if needle and needle not in label.lower():
                continue
            rows.append(label)
        rows.sort()
        for label in rows:
            self.accounts_list.addItem(QListWidgetItem(label))
        self.accounts_count.setText(f"{len(rows)} account(s)")

    # --------------------------------------------------------------- save
    def _save(self) -> None:
        save_config(self._cfg)
        starred = len(self._cfg.marketing_program_starred or [])
        cats = len(self._cfg.marketing_program_categories or [])
        mapped = len(self._cfg.marketing_program_category_by_code or {})
        self.status.setText(
            f"Saved · {starred} starred · {mapped} categorized · {cats} categor(ies) available."
        )
