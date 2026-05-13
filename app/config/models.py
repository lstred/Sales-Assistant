"""Pydantic models describing all non-secret configuration.

Secrets (DB passwords if any, SMTP password, IMAP password, AI API keys)
are stored in Windows Credential Manager via ``keyring`` and referenced
here only by their *username* / handle.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DatabaseConfig(BaseModel):
    """SQL Server connection (Trusted_Connection — no password on disk)."""

    driver: str = "ODBC Driver 18 for SQL Server"
    server: str = "NRFVMSSQL04"
    database: str = "NRF_REPORTS"
    trusted_connection: bool = True
    encrypt: Literal["yes", "no", "strict"] = "no"

    def odbc_connection_string(self) -> str:
        parts = [
            f"Driver={{{self.driver}}}",
            f"Server={self.server}",
            f"Database={self.database}",
            "Trusted_Connection=Yes" if self.trusted_connection else "Trusted_Connection=No",
            f"Encrypt={self.encrypt}",
        ]
        return ";".join(parts) + ";"


class EmailConfig(BaseModel):
    """SMTP (send) + IMAP (receive) configuration.

    Passwords live in keyring under service ``SalesAssistant/SMTP`` and
    ``SalesAssistant/IMAP`` keyed by ``smtp_username`` / ``imap_username``.
    """

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_starttls: bool = True
    smtp_username: str = ""
    smtp_from_address: str = ""
    smtp_from_name: str = "Sales Assistant"

    imap_host: str = ""
    imap_port: int = 993
    imap_ssl: bool = True
    imap_username: str = ""
    imap_mailbox: str = "INBOX"

    # Safety: until enabled, every outbound email goes to the manager for
    # manual review. NEVER default this to True.
    enable_outbound_send: bool = False
    # If non-empty, every send goes to this address INSTEAD OF the rep
    # (great for early dry-runs).
    redirect_all_to: str = ""


class AIConfig(BaseModel):
    """AI provider configuration. API key lives in keyring under
    ``SalesAssistant/AI`` keyed by ``provider`` value (e.g. ``openai``)."""

    provider: Literal["openai", "anthropic", "azure_openai"] = "openai"
    model: str = "gpt-4.1-mini"
    base_url: str = ""  # blank => provider default
    api_username: str = "default"  # keyring username (lets you store multiple keys)
    request_timeout_seconds: int = 60
    max_output_tokens: int = 1500
    temperature: float = 0.4


class ScheduleConfig(BaseModel):
    enabled: bool = False
    cron: str = "0 7 * * MON"  # 7 AM Mondays
    quiet_hours_start: str = "18:00"
    quiet_hours_end: str = "07:00"


class AppConfig(BaseModel):
    schema_version: int = 1
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)

    # User-maintained mappings (free-form, edited in UI later):
    sample_to_product_cc: dict[str, str] = Field(default_factory=dict)
    """Map sample cost-center code -> product cost-center code."""

    core_displays_by_cc: dict[str, list[str]] = Field(default_factory=dict)
    """Map cost-center code -> list of CLASSES.CLCODE values that count as 'core' displays."""

    rep_emails: dict[str, str] = Field(default_factory=dict)
    """Map SALESMAN.YSLMN# -> email address (overrides any other lookup)."""

    rep_boss_emails: dict[str, str] = Field(default_factory=dict)
    """Map SALESMAN.YSLMN# -> boss/escalation CC address."""

    rep_tone: dict[str, int] = Field(default_factory=dict)
    """Map SALESMAN.YSLMN# -> tone bias on -3 (stick) .. +3 (carrot) scale."""
