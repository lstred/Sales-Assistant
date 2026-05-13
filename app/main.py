"""Application entry point."""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app import __app_name__
from app.app_paths import logs_dir
from app.config.store import load_config
from app.storage.db import init_db
from app.ui.main_window import MainWindow
from app.ui.theme import apply_theme


def _configure_logging() -> None:
    log_path = logs_dir() / "app.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )


def main() -> int:
    _configure_logging()
    log = logging.getLogger(__name__)
    log.info("Starting %s", __app_name__)

    # Make the app DPI-aware before QApplication is constructed
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName(__app_name__)
    app.setOrganizationName("NRF")
    apply_theme(app)

    # Local SQLite app-state DB (conversations, messages, etc.)
    init_db()

    # Load (or create) config
    cfg = load_config()

    window = MainWindow(cfg)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
