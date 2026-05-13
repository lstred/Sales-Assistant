"""Config persistence + secret access (Windows Credential Manager via keyring)."""

from __future__ import annotations

import json
import logging
from typing import Any

import keyring
from keyring.errors import KeyringError

from app.app_paths import config_path
from app.config.models import AppConfig

log = logging.getLogger(__name__)

KEYRING_SERVICE_PREFIX = "SalesAssistant"


# ----------------------------------------------------------------------- config
def load_config() -> AppConfig:
    """Load config from %APPDATA%; on first run write defaults."""
    path = config_path()
    if not path.exists():
        cfg = AppConfig()
        save_config(cfg)
        return cfg
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return AppConfig.model_validate(raw)
    except Exception:  # noqa: BLE001 — config files can be corrupted; fall back gracefully
        log.exception("Failed to parse %s; falling back to defaults", path)
        return AppConfig()


def save_config(cfg: AppConfig) -> None:
    path = config_path()
    path.write_text(
        json.dumps(cfg.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )


# ----------------------------------------------------------------------- secrets
def _service(name: str) -> str:
    return f"{KEYRING_SERVICE_PREFIX}/{name}"


def get_secret(category: str, username: str) -> str | None:
    """Read a secret from Windows Credential Manager."""
    if not username:
        return None
    try:
        return keyring.get_password(_service(category), username)
    except KeyringError:
        log.exception("Keyring read failed for %s/%s", category, username)
        return None


def set_secret(category: str, username: str, value: str) -> None:
    """Write/overwrite a secret in Windows Credential Manager."""
    if not username:
        raise ValueError("username is required to store a secret")
    keyring.set_password(_service(category), username, value)


def delete_secret(category: str, username: str) -> None:
    if not username:
        return
    try:
        keyring.delete_password(_service(category), username)
    except KeyringError:
        # Already absent or backend doesn't support delete — ignore.
        pass
