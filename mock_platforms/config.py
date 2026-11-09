from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MockPlatformsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOCK_PLATFORMS_", env_file=".env", extra="ignore")

    token: SecretStr = Field(min_length=1)
    port: int = 8100
    delivery_dir: Path = Path("data/generated/delivery")

    faults_enabled: bool = True
    fault_seed: int = 42
    rate_429: float = Field(default=0.05, ge=0)
    rate_500: float = Field(default=0.02, ge=0)
    retry_after_seconds: int = Field(default=1, ge=0)

    @model_validator(mode="after")
    def rates_fit_in_one(self) -> Self:
        if self.rate_429 + self.rate_500 > 1:
            raise ValueError("rate_429 + rate_500 must be at most 1")
        return self
