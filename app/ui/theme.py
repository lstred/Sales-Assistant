"""Premium QSS theme + palette helpers.

Design language:
* Light content area with a deep, near-black sidebar.
* Single accent color used sparingly (active nav, primary buttons, focus rings).
* Segoe UI Variable as primary typeface (falls back gracefully).
* 8-px spacing grid; subtle shadows replaced by 1-px borders for a flat,
  premium feel.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

# ---------------------------------------------------------------- color tokens
ACCENT = "#2563EB"            # primary accent (blue-600)
ACCENT_HOVER = "#1D4ED8"      # blue-700
ACCENT_PRESSED = "#1E40AF"    # blue-800
ACCENT_SUBTLE = "#E0E7FF"     # blue-100 — focus/selection wash

BG = "#F7F8FA"                # main content background
SURFACE = "#FFFFFF"           # cards, tables, inputs
SURFACE_ALT = "#F1F2F6"       # hover/zebra
BORDER = "#E2E5EB"
BORDER_STRONG = "#CBD0D9"

TEXT = "#0F172A"              # slate-900
TEXT_MUTED = "#64748B"        # slate-500
TEXT_INVERSE = "#F8FAFC"

SIDEBAR_BG = "#0F172A"        # slate-900
SIDEBAR_BG_HOVER = "#1E293B"  # slate-800
SIDEBAR_BG_ACTIVE = "#1F2937" # slate-800/900 mix
SIDEBAR_TEXT = "#CBD5E1"      # slate-300
SIDEBAR_TEXT_ACTIVE = "#FFFFFF"
SIDEBAR_ACCENT = ACCENT

DANGER = "#DC2626"
WARN = "#D97706"
SUCCESS = "#16A34A"


def apply_theme(app: QApplication) -> None:
    _install_fonts(app)
    _install_palette(app)
    app.setStyleSheet(_QSS)


def _install_fonts(app: QApplication) -> None:
    # Prefer Segoe UI Variable on Windows 11 / current Windows builds, then
    # standard Segoe UI as a strong fallback.
    families = QFontDatabase.families()
    preferred = None
    for name in ("Segoe UI Variable", "Segoe UI Variable Display", "Segoe UI"):
        if name in families:
            preferred = name
            break
    base = QFont(preferred or app.font().family(), 10)
    base.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(base)


def _install_palette(app: QApplication) -> None:
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(BG))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Base, QColor(SURFACE))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(SURFACE_ALT))
    pal.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Button, QColor(SURFACE))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(TEXT_INVERSE))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(TEXT_MUTED))
    app.setPalette(pal)


_QSS = f"""
* {{
    color: {TEXT};
    selection-background-color: {ACCENT};
    selection-color: {TEXT_INVERSE};
    outline: 0;
}}

QMainWindow, QWidget#contentRoot {{
    background: {BG};
}}

/* ---------------- Sidebar ---------------- */
QFrame#sidebar {{
    background: {SIDEBAR_BG};
    border: none;
}}
QLabel#sidebarBrand {{
    color: {TEXT_INVERSE};
    font-size: 16px;
    font-weight: 600;
    padding: 18px 20px 4px 20px;
    letter-spacing: 0.2px;
}}
QLabel#sidebarTagline {{
    color: {SIDEBAR_TEXT};
    font-size: 11px;
    padding: 0px 20px 18px 20px;
}}
QPushButton#navButton {{
    background: transparent;
    color: {SIDEBAR_TEXT};
    border: none;
    border-left: 3px solid transparent;
    padding: 10px 18px;
    text-align: left;
    font-size: 13px;
    font-weight: 500;
}}
QPushButton#navButton:hover {{
    background: {SIDEBAR_BG_HOVER};
    color: {TEXT_INVERSE};
}}
QPushButton#navButton:checked {{
    background: {SIDEBAR_BG_ACTIVE};
    color: {SIDEBAR_TEXT_ACTIVE};
    border-left: 3px solid {SIDEBAR_ACCENT};
}}

/* ---------------- Header / titles ---------------- */
QLabel#viewTitle {{
    font-size: 22px;
    font-weight: 600;
    padding: 4px 0 0 0;
}}
QLabel#viewSubtitle {{
    font-size: 12px;
    color: {TEXT_MUTED};
    padding-bottom: 12px;
}}

/* ---------------- Cards ---------------- */
QFrame#card {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QLabel#cardTitle {{
    font-size: 11px;
    font-weight: 600;
    color: {TEXT_MUTED};
    text-transform: uppercase;
    letter-spacing: 0.6px;
}}
QLabel#cardValue {{
    font-size: 26px;
    font-weight: 600;
    color: {TEXT};
}}
QLabel#cardCaption {{
    font-size: 11px;
    color: {TEXT_MUTED};
}}

/* ---------------- Inputs ---------------- */
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 10px;
    selection-background-color: {ACCENT};
    selection-color: {TEXT_INVERSE};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus,
QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {ACCENT};
}}
QComboBox::drop-down {{
    border: none;
    width: 22px;
}}

/* ---------------- Buttons ---------------- */
QPushButton {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER_STRONG};
    border-radius: 6px;
    padding: 7px 14px;
    font-weight: 500;
}}
QPushButton:hover {{ background: {SURFACE_ALT}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {TEXT_MUTED}; background: {SURFACE_ALT}; }}

QPushButton[primary="true"] {{
    background: {ACCENT};
    color: {TEXT_INVERSE};
    border: 1px solid {ACCENT};
}}
QPushButton[primary="true"]:hover  {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton[primary="true"]:pressed{{ background: {ACCENT_PRESSED}; border-color: {ACCENT_PRESSED}; }}

QPushButton[danger="true"] {{
    background: {SURFACE};
    color: {DANGER};
    border-color: {BORDER_STRONG};
}}
QPushButton[danger="true"]:hover {{
    background: #FEF2F2;
    border-color: {DANGER};
}}

/* ---------------- Tables ---------------- */
QTableView, QTreeView, QListView {{
    background: {SURFACE};
    alternate-background-color: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    gridline-color: {BORDER};
    selection-background-color: {ACCENT_SUBTLE};
    selection-color: {TEXT};
}}
QHeaderView::section {{
    background: {SURFACE_ALT};
    color: {TEXT_MUTED};
    padding: 8px 10px;
    border: none;
    border-bottom: 1px solid {BORDER};
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.4px;
}}

/* ---------------- Tabs ---------------- */
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 8px;
    background: {SURFACE};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_MUTED};
    padding: 8px 14px;
    margin-right: 2px;
    border: none;
    border-bottom: 2px solid transparent;
    font-weight: 500;
}}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{
    color: {ACCENT};
    border-bottom: 2px solid {ACCENT};
}}

/* ---------------- Group boxes ---------------- */
QGroupBox {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 14px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 6px;
    color: {TEXT_MUTED};
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}}

/* ---------------- Status bar ---------------- */
QStatusBar {{
    background: {SURFACE};
    border-top: 1px solid {BORDER};
    color: {TEXT_MUTED};
}}
QStatusBar QLabel {{ color: {TEXT_MUTED}; padding: 0 8px; }}

/* ---------------- Scrollbars ---------------- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 0; }}
QScrollBar::handle:vertical {{ background: {BORDER_STRONG}; border-radius: 5px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {TEXT_MUTED}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0 4px; }}
QScrollBar::handle:horizontal {{ background: {BORDER_STRONG}; border-radius: 5px; min-width: 24px; }}
QScrollBar::handle:horizontal:hover {{ background: {TEXT_MUTED}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ---------------- Tooltips ---------------- */
QToolTip {{
    background: {SIDEBAR_BG};
    color: {TEXT_INVERSE};
    border: 1px solid {SIDEBAR_BG};
    border-radius: 4px;
    padding: 4px 8px;
}}

/* ---------------- Dialog ---------------- */
QDialog {{ background: {BG}; }}
"""
