"""Application configuration.

All settings come from environment variables (optionally loaded from a ``.env``
file). Business-specific content lives in ``config/business_profile.yaml`` and
``config/qualification_rules.yaml`` so it can be edited without code changes.

Safety default: when real credentials are missing, WhatsApp and the LLM run in
*mock* mode, so development never sends real messages by accident.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

try:  # python-dotenv is optional at runtime
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def _env_list(name: str, default: str) -> list[str]:
    return [p.strip() for p in _env(name, default).split(",") if p.strip()]


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else PROJECT_ROOT / p


@dataclass
class Settings:
    # --- general -----------------------------------------------------------
    app_env: str = "development"
    log_level: str = "INFO"
    database_path: Path = field(default_factory=lambda: _path("data/agent.db"))
    admin_api_key: str = ""

    # --- lead source ---------------------------------------------------------
    lead_source: str = "csv"  # csv | google_sheets
    leads_csv_path: Path = field(default_factory=lambda: _path("data/leads.csv"))
    csv_poll_interval_seconds: float = 5.0
    csv_settle_seconds: float = 1.0
    csv_process_existing_on_startup: bool = False
    csv_reprocess_edited_rows: bool = False
    csv_use_watchdog: bool = True
    duplicate_policy: str = "review"  # off | flag | review
    duplicate_window_days: int = 30
    consent_required: bool = True
    consent_accepted_values: list[str] = field(
        default_factory=lambda: ["OPTED_IN", "YES", "TRUE", "CONSENTED", "Y"]
    )
    default_country_code: str = "91"

    # --- WhatsApp Cloud API -----------------------------------------------------
    whatsapp_mode: str = "auto"  # auto | mock | live
    whatsapp_access_token: str = ""
    whatsapp_business_account_id: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_graph_api_version: str = "v26.0"
    whatsapp_graph_base_url: str = "https://graph.facebook.com"
    whatsapp_company_phone: str = ""
    whatsapp_template_name: str = "lead_introduction"
    whatsapp_template_language: str = "en"
    whatsapp_template_params: list[str] = field(
        default_factory=lambda: ["customer_name", "requirement"]
    )
    whatsapp_send_rate_per_second: float = 5.0
    whatsapp_timeout_seconds: float = 15.0
    customer_service_window_hours: int = 24
    whatsapp_test_allowlist: list[str] = field(default_factory=list)

    # --- LLM ---------------------------------------------------------------------
    llm_mode: str = "auto"  # auto | mock | live
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"
    anthropic_max_tokens: int = 1500
    anthropic_timeout_seconds: float = 60.0
    anthropic_max_retries: int = 2
    history_messages_for_llm: int = 20

    # --- outputs -----------------------------------------------------------------
    output_sinks: list[str] = field(default_factory=lambda: ["csv"])
    google_sheets_spreadsheet_id: str = ""
    google_sheets_worksheet: str = "Leads"
    google_service_account_file: str = ""
    google_sheets_key_column: str = "lead_id"
    google_sheets_poll_interval_seconds: float = 60.0

    # --- knowledge base ------------------------------------------------------------
    business_profile_path: Path = field(
        default_factory=lambda: _path("config/business_profile.yaml")
    )
    qualification_rules_path: Path = field(
        default_factory=lambda: _path("config/qualification_rules.yaml")
    )
    knowledge_base_dir: Path = field(default_factory=lambda: _path("data/knowledge_base"))
    price_max_age_days: int = 180
    kb_refresh_interval_seconds: float = 300.0
    ocr_enabled: bool = False
    ocr_language: str = "eng"

    # --- conversations / workers ---------------------------------------------------
    inactivity_close_minutes: int = 30
    no_response_close_hours: int = 72
    reopen_on_inbound: bool = True
    worker_poll_interval_seconds: float = 1.0
    job_max_attempts: int = 5
    job_backoff_base_seconds: float = 5.0
    job_stale_lock_seconds: int = 300
    retention_days: int = 0  # 0 disables automatic purge

    # --- notifications -------------------------------------------------------------
    notify_webhook_url: str = ""
    notify_timeout_seconds: float = 10.0

    # ------------------------------------------------------------------------------
    @property
    def whatsapp_live(self) -> bool:
        if self.whatsapp_mode == "live":
            return True
        if self.whatsapp_mode == "mock":
            return False
        return bool(self.whatsapp_access_token and self.whatsapp_phone_number_id)

    @property
    def llm_live(self) -> bool:
        if self.llm_mode == "live":
            return True
        if self.llm_mode == "mock":
            return False
        return bool(self.anthropic_api_key)

    def secret_values(self) -> list[str]:
        """Values that must never appear in logs or customer messages."""
        return [
            s
            for s in (
                self.whatsapp_access_token,
                self.whatsapp_app_secret,
                self.whatsapp_verify_token,
                self.anthropic_api_key,
                self.admin_api_key,
            )
            if s and len(s) >= 6
        ]

    def validate(self) -> list[str]:
        """Return configuration problems (empty list == OK)."""
        problems: list[str] = []
        if self.whatsapp_mode == "live":
            for name in ("whatsapp_access_token", "whatsapp_phone_number_id",
                         "whatsapp_verify_token", "whatsapp_app_secret"):
                if not getattr(self, name):
                    problems.append(f"WHATSAPP_MODE=live but {name.upper()} is empty")
        if self.llm_mode == "live" and not self.anthropic_api_key:
            problems.append("LLM_MODE=live but ANTHROPIC_API_KEY is empty")
        if "sheets" in self.output_sinks or self.lead_source == "google_sheets":
            if not self.google_sheets_spreadsheet_id:
                problems.append("Google Sheets enabled but GOOGLE_SHEETS_SPREADSHEET_ID is empty")
            if not self.google_service_account_file:
                problems.append("Google Sheets enabled but GOOGLE_SERVICE_ACCOUNT_FILE is empty")
        if self.duplicate_policy not in {"off", "flag", "review"}:
            problems.append("DUPLICATE_POLICY must be off, flag or review")
        for sink in self.output_sinks:
            if sink not in {"csv", "sheets"}:
                problems.append(f"Unknown OUTPUT_SINKS entry: {sink}")
        return problems


def load_settings(env_file: str | None = None) -> Settings:
    if load_dotenv is not None:
        load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    return Settings(
        app_env=_env("APP_ENV", "development"),
        log_level=_env("LOG_LEVEL", "INFO"),
        database_path=_path(_env("DATABASE_PATH", "data/agent.db")),
        admin_api_key=_env("ADMIN_API_KEY"),
        lead_source=_env("LEAD_SOURCE", "csv"),
        leads_csv_path=_path(_env("LEADS_CSV_PATH", "data/leads.csv")),
        csv_poll_interval_seconds=_env_float("CSV_POLL_INTERVAL_SECONDS", 5.0),
        csv_settle_seconds=_env_float("CSV_SETTLE_SECONDS", 1.0),
        csv_process_existing_on_startup=_env_bool("CSV_PROCESS_EXISTING_ON_STARTUP", False),
        csv_reprocess_edited_rows=_env_bool("CSV_REPROCESS_EDITED_ROWS", False),
        csv_use_watchdog=_env_bool("CSV_USE_WATCHDOG", True),
        duplicate_policy=_env("DUPLICATE_POLICY", "review").lower(),
        duplicate_window_days=_env_int("DUPLICATE_WINDOW_DAYS", 30),
        consent_required=_env_bool("CONSENT_REQUIRED", True),
        consent_accepted_values=[v.upper() for v in _env_list(
            "CONSENT_ACCEPTED_VALUES", "OPTED_IN,YES,TRUE,CONSENTED,Y")],
        default_country_code=_env("DEFAULT_COUNTRY_CODE", "91"),
        whatsapp_mode=_env("WHATSAPP_MODE", "auto").lower(),
        whatsapp_access_token=_env("WHATSAPP_ACCESS_TOKEN"),
        whatsapp_business_account_id=_env("WHATSAPP_BUSINESS_ACCOUNT_ID"),
        whatsapp_phone_number_id=_env("WHATSAPP_PHONE_NUMBER_ID"),
        whatsapp_verify_token=_env("WHATSAPP_VERIFY_TOKEN"),
        whatsapp_app_secret=_env("WHATSAPP_APP_SECRET"),
        whatsapp_graph_api_version=_env("WHATSAPP_GRAPH_API_VERSION", "v26.0"),
        whatsapp_graph_base_url=_env("WHATSAPP_GRAPH_BASE_URL", "https://graph.facebook.com"),
        whatsapp_company_phone=_env("WHATSAPP_COMPANY_PHONE"),
        whatsapp_template_name=_env("WHATSAPP_TEMPLATE_NAME", "lead_introduction"),
        whatsapp_template_language=_env("WHATSAPP_TEMPLATE_LANGUAGE", "en"),
        whatsapp_template_params=_env_list("WHATSAPP_TEMPLATE_PARAMS", "customer_name,requirement"),
        whatsapp_send_rate_per_second=_env_float("WHATSAPP_SEND_RATE_PER_SECOND", 5.0),
        whatsapp_timeout_seconds=_env_float("WHATSAPP_TIMEOUT_SECONDS", 15.0),
        customer_service_window_hours=_env_int("CUSTOMER_SERVICE_WINDOW_HOURS", 24),
        whatsapp_test_allowlist=_env_list("WHATSAPP_TEST_ALLOWLIST", ""),
        llm_mode=_env("LLM_MODE", "auto").lower(),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        anthropic_model=_env("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
        anthropic_max_tokens=_env_int("ANTHROPIC_MAX_TOKENS", 1500),
        anthropic_timeout_seconds=_env_float("ANTHROPIC_TIMEOUT_SECONDS", 60.0),
        anthropic_max_retries=_env_int("ANTHROPIC_MAX_RETRIES", 2),
        history_messages_for_llm=_env_int("HISTORY_MESSAGES_FOR_LLM", 20),
        output_sinks=[s.lower() for s in _env_list("OUTPUT_SINKS", "csv")],
        google_sheets_spreadsheet_id=_env("GOOGLE_SHEETS_SPREADSHEET_ID"),
        google_sheets_worksheet=_env("GOOGLE_SHEETS_WORKSHEET", "Leads"),
        google_service_account_file=_env("GOOGLE_SERVICE_ACCOUNT_FILE"),
        google_sheets_key_column=_env("GOOGLE_SHEETS_KEY_COLUMN", "lead_id"),
        google_sheets_poll_interval_seconds=_env_float("GOOGLE_SHEETS_POLL_INTERVAL_SECONDS", 60.0),
        business_profile_path=_path(_env("BUSINESS_PROFILE_PATH", "config/business_profile.yaml")),
        qualification_rules_path=_path(_env("QUALIFICATION_RULES_PATH", "config/qualification_rules.yaml")),
        knowledge_base_dir=_path(_env("KNOWLEDGE_BASE_DIR", "data/knowledge_base")),
        price_max_age_days=_env_int("PRICE_MAX_AGE_DAYS", 180),
        kb_refresh_interval_seconds=_env_float("KB_REFRESH_INTERVAL_SECONDS", 300.0),
        ocr_enabled=_env_bool("OCR_ENABLED", False),
        ocr_language=_env("OCR_LANGUAGE", "eng"),
        inactivity_close_minutes=_env_int("INACTIVITY_CLOSE_MINUTES", 30),
        no_response_close_hours=_env_int("NO_RESPONSE_CLOSE_HOURS", 72),
        reopen_on_inbound=_env_bool("REOPEN_ON_INBOUND", True),
        worker_poll_interval_seconds=_env_float("WORKER_POLL_INTERVAL_SECONDS", 1.0),
        job_max_attempts=_env_int("JOB_MAX_ATTEMPTS", 5),
        job_backoff_base_seconds=_env_float("JOB_BACKOFF_BASE_SECONDS", 5.0),
        job_stale_lock_seconds=_env_int("JOB_STALE_LOCK_SECONDS", 300),
        retention_days=_env_int("RETENTION_DAYS", 0),
        notify_webhook_url=_env("NOTIFY_WEBHOOK_URL"),
        notify_timeout_seconds=_env_float("NOTIFY_TIMEOUT_SECONDS", 10.0),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


# ---------------------------------------------------------------------------
# Business profile
# ---------------------------------------------------------------------------

PLACEHOLDER_MARKER = "CONFIGURE_ME"


@dataclass
class BusinessProfile:
    data: dict[str, Any]

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return default if node in (None, "") else node

    def known(self, dotted: str) -> Any:
        """Return a value only if it is configured (not a placeholder)."""
        value = self.get(dotted)
        if isinstance(value, str) and PLACEHOLDER_MARKER in value:
            return None
        return value

    @property
    def company_name(self) -> str:
        return self.known("company.name") or "our company"

    def unconfigured_fields(self) -> list[str]:
        found: list[str] = []

        def walk(node: Any, prefix: str) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, f"{prefix}.{k}" if prefix else k)
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f"{prefix}[{i}]")
            elif isinstance(node, str) and PLACEHOLDER_MARKER in node:
                found.append(prefix)

        walk(self.data, "")
        return found


def load_yaml(path: Path, fallback: Path | None = None) -> dict[str, Any]:
    candidates = [path]
    if fallback is not None:
        candidates.append(fallback)
    for candidate in candidates:
        if candidate.exists():
            with candidate.open("r", encoding="utf-8") as fh:
                return yaml.safe_load(fh) or {}
    return {}


def load_business_profile(settings: Settings) -> BusinessProfile:
    example = settings.business_profile_path.with_name("business_profile.example.yaml")
    return BusinessProfile(load_yaml(settings.business_profile_path, example))


def load_qualification_rules(settings: Settings) -> dict[str, Any]:
    example = settings.qualification_rules_path.with_name("qualification_rules.example.yaml")
    return load_yaml(settings.qualification_rules_path, example)
