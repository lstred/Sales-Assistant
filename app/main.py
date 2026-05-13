"""Application entry point."""

from __future__ import annotations

import logging
import sys
import traceback

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
    handlers: list[logging.Handler] = [logging.FileHandler(log_path, encoding="utf-8")]
    # pythonw.exe sets sys.stderr/sys.stdout to None; only attach a stream
    # handler when one is actually usable.
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def _install_excepthook() -> None:
    """Route unhandled exceptions to the log file (pythonw swallows them)."""
    log = logging.getLogger("app.unhandled")

    def _hook(exc_type, exc, tb):
        log.error(
            "Unhandled exception:\n%s",
            "".join(traceback.format_exception(exc_type, exc, tb)),
        )

    sys.excepthook = _hook


def main() -> int:
    _configure_logging()
    _install_excepthook()
    log = logging.getLogger(__name__)
    log.info("Starting %s", __app_name__)

    # Make the app DPI-aware before QApplication is constructed.
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    # pythonw passes argv with no console; make sure argv[0] is at least a string.
    argv = sys.argv if sys.argv else [""]
    app = QApplication(argv)
    app.setApplicationName(__app_name__)
    app.setOrganizationName("NRF")
    apply_theme(app)

    # Local SQLite app-state DB (conversations, messages, etc.)
    init_db()

    cfg = load_config()

    window = MainWindow(cfg)
    window.show()
    window.raise_()
    window.activateWindow()
    log.info("MainWindow shown (visible=%s)", window.isVisible())
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
