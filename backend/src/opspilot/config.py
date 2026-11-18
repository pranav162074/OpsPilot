from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
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

    # Creating a workspace needs this token. Unset means workspace creation is switched off.
    admin_token: SecretStr | None = None

    # Requests per minute for each API key, and workspace creations per hour for each IP
    api_rate_limit_per_minute: int = 120
    workspace_create_limit_per_hour: int = 10

    @field_validator("admin_token", mode="before")
    @classmethod
    def _blank_token_means_unset(cls, value: object) -> object:
        # ADMIN_TOKEN= in .env must switch creation off, not accept an empty header
        if isinstance(value, str) and not value.strip():
            return None
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
