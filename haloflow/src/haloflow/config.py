"""
HaloFlow — runtime configuration.

All values come from environment variables (or .env file in dev).
Nothing sensitive is hardcoded here.
"""
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── App ──────────────────────────────────────────────────────────────────
    app_env: str = "development"
    log_level: str = "INFO"
    tenant_id: str = "pilot-clinic-1"
    webhook_secret: str = "changeme"

    # ── Database ─────────────────────────────────────────────────────────────
    database_url: str  # postgresql+asyncpg://...

    # ── athenahealth ─────────────────────────────────────────────────────────
    athena_client_id: str = ""
    athena_client_secret: str = ""
    athena_practice_id: str = ""
    athena_base_url: str = "https://api.preview.platform.athenahealth.com"

    # ── Notifyre (SMS + fax) ──────────────────────────────────────────────────
    notifyre_api_key: str = ""
    notifyre_sms_from: str = ""
    notifyre_fax_inbound_number: str = ""
    notifyre_fax_from_number: str = ""

    # ── Stedi eligibility ─────────────────────────────────────────────────────
    stedi_api_key: str = ""

    # ── Scheduling defaults ────────────────────────────────────────────────────
    reminder_days_before: int = 2
    eligibility_days_before: int = 3
    no_show_rebook_hours: int = 24

    # ── Priority payers (Stedi payer IDs, comma-separated in env) ─────────────
    priority_payer_ids: list[str] = ["ANTHM", "UHC", "SNTARA"]

    @field_validator("priority_payer_ids", mode="before")
    @classmethod
    def parse_payer_ids(cls, v: object) -> list[str]:
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return list(v)  # type: ignore[arg-type]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
