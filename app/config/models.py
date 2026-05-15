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


class FiscalCalendarConfig(BaseModel):
    """Overrides for the NRF 4-4-5 fiscal calendar.

    Defaults are correct: FY starts Sunday Feb 1; months are 4-4-5 weeks
    repeating; FY27 anchor is Feb 1 2026. The only thing that varies is the
    rare 6-week January used to realign with the calendar year — list those
    fiscal-year labels here.
    """

    six_week_january_years: list[int] = Field(default_factory=list)
    """Fiscal years (e.g. 2027) where January is 6 weeks instead of 5."""


class BudgetConfig(BaseModel):
    """Budget / forecast settings.

    ``budget_fiscal_year`` is the FY label (e.g. 2027) the budget is being
    built for.  0 means auto-detect (current fiscal year).

    ``cc_growth_pct`` maps product cost-center code → percentage growth or
    contraction vs the prior fiscal year (e.g. 10.0 for +10 %, -5.0 for −5 %).
    CCs not in this map default to 0 % growth.

    ``monthly_seasonality_pct`` has 12 values for P1 (February) through
    P12 (January).  Values should sum to 100.0.  The default is a flat
    distribution (8.34 / 8.33 alternating, sums to 100.0).
    """

    budget_fiscal_year: int = 0
    cc_growth_pct: dict[str, float] = Field(default_factory=dict)
    monthly_seasonality_pct: list[float] = Field(
        default_factory=lambda: [
            8.34, 8.33, 8.33, 8.34, 8.33, 8.33,
            8.34, 8.33, 8.33, 8.34, 8.33, 8.33,
        ]
    )
    rep_cc_growth_pct_saved: dict[str, dict[str, float]] = Field(default_factory=dict)
    """Persisted rep-level growth overrides from the last upload.
    Outer key = rep_number (str), inner = cc_code → growth_pct.
    Serialised as nested JSON so it survives app restarts."""


class GlobalFiltersConfig(BaseModel):
    """App-wide default filters applied to every sales-driven view.

    Stored as ISO date strings (or empty for "auto = last 12 fiscal periods")
    so JSON round-trips cleanly. Cost centers is a list of CC code strings;
    empty list means "all CCs". ``vs_prior_year`` toggles the comparison
    parallel-load.
    """

    start_iso: str = ""           # "" => last 12 fiscal periods (auto)
    end_iso: str = ""             # "" => auto (paired with start)
    cost_centers: list[str] = Field(default_factory=list)
    vs_prior_year: bool = True


class PageFilterDefault(BaseModel):
    """Per-page saved filter state.

    ``start_relative`` / ``end_relative`` hold a relative-date token (e.g.
    ``"today"``, ``"yesterday"``, ``"fiscal_ytd_start"``) so the date is
    re-evaluated fresh every time the page loads.  When set, they take
    priority over the ISO strings.  Empty string means "use the ISO date".
    """

    start_relative: str = ""    # relative token or ""
    end_relative: str = ""      # relative token or ""
    start_iso: str = ""         # absolute fallback / override
    end_iso: str = ""
    cost_centers: list[str] = Field(default_factory=list)
    vs_prior_year: bool = True


class AppConfig(BaseModel):
    schema_version: int = 1
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    fiscal: FiscalCalendarConfig = Field(default_factory=FiscalCalendarConfig)
    defaults: GlobalFiltersConfig = Field(default_factory=GlobalFiltersConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    page_defaults: dict[str, PageFilterDefault] = Field(default_factory=dict)
    """Per-page saved filter defaults keyed by page_id string."""

    # User-maintained mappings (free-form, edited in UI later):
    sample_to_product_cc: dict[str, str] = Field(default_factory=dict)
    """Map sample cost-center code (starts with '1') -> product cost-center
    code (starts with '0')."""

    core_displays_by_cc: dict[str, list[str]] = Field(default_factory=dict)
    """Map cost-center code -> list of CLASSES.CLCODE values that count as 'core' displays."""

    rep_emails: dict[str, str] = Field(default_factory=dict)
    """Map SALESMAN.YSLMN# -> email address (overrides any other lookup)."""

    rep_boss_emails: dict[str, str] = Field(default_factory=dict)
    """Map SALESMAN.YSLMN# -> boss/escalation CC address."""

    rep_tone: dict[str, int] = Field(default_factory=dict)
    """Map SALESMAN.YSLMN# -> tone bias on -3 (stick) .. +3 (carrot) scale."""
