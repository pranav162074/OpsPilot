from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables (or a local .env file)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://opspilot:opspilot@localhost:5433/opspilot"
    redis_url: str = "redis://localhost:6380/0"

    # A readiness check that takes longer than this counts as down
    health_check_timeout_seconds: float = 2.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
