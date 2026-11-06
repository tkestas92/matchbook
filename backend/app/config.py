from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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


@lru_cache
def get_settings() -> Settings:
    return Settings()
