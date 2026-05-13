"""Resolved filesystem paths for application data.

All persistent app data lives under ``%APPDATA%\\SalesAssistant\\`` so the
.exe build doesn't need write access to its install directory.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_FOLDER_NAME = "SalesAssistant"


def appdata_dir() -> Path:
    """Return the per-user application data directory, creating it if needed."""
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    path = Path(base) / APP_FOLDER_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return appdata_dir() / "config.json"


def state_db_path() -> Path:
    return appdata_dir() / "state.sqlite"


def logs_dir() -> Path:
    p = appdata_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p
