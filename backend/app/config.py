from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelPrice(BaseModel):
    """List price in EUR per million tokens (decision 039)."""

    input_eur_per_mtok: Decimal
    output_eur_per_mtok: Decimal


# Paid list price (USD) used as EUR, so the caps err high. Check against the provider's price page.
DEFAULT_LLM_PRICES = {
    "gemini-2.5-flash-lite": ModelPrice(
        input_eur_per_mtok=Decimal("0.10"), output_eur_per_mtok=Decimal("0.40")
    ),
    "gemini-3.1-flash-lite": ModelPrice(
        input_eur_per_mtok=Decimal("0.25"), output_eur_per_mtok=Decimal("1.50")
    ),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    app_env: str = "local"
    agency_timezone: str = "Europe/Vilnius"

    # Login passwords for the least-privilege DB roles (`make roles`).
    etl_db_password: SecretStr | None = None
    app_db_password: SecretStr | None = None
    agent_ro_db_password: SecretStr | None = None
    eval_runner_db_password: SecretStr | None = None
    demo_ro_db_password: SecretStr | None = None

    # LLM access layer (backend/app/llm/, decisions 039, 040 and 041).
    llm_provider: Literal["gemini"] = "gemini"
    llm_model: str = "gemini-3.1-flash-lite"
    gemini_api_key: SecretStr | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    llm_timeout_seconds: float = 30.0
    llm_max_input_chars: int = 50_000
    llm_max_output_tokens: int = 2048
    llm_max_response_bytes: int = 256_000
    llm_run_cap_eur: Decimal = Decimal("0.20")
    llm_monthly_cap_eur: Decimal = Decimal("1")
    llm_prices_eur_per_mtok: dict[str, ModelPrice] = DEFAULT_LLM_PRICES
    llm_retry_base_seconds: float = 1.0
    llm_retry_cap_seconds: float = 30.0
    llm_retry_attempts_429: int = 6
    llm_retry_attempts_5xx: int = 4
    llm_max_retry_wait_seconds: float = 60.0

    # LLM call tracing (backend/app/llm/tracing.py, decision 048): off unless all three are set.
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str | None = None
    langfuse_timeout_seconds: float = 2.0
    langfuse_max_consecutive_failures: int = 3

    # Invoice extraction (backend/app/extraction/, decision 042).
    extraction_confidence_threshold: Decimal = Decimal("0.80")
    extraction_max_pdf_bytes: int = 5_000_000
    extraction_max_pages: int = 5


@lru_cache
def get_settings() -> Settings:
    return Settings()
