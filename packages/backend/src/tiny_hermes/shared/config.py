from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str
    redis_url: str
    s3_endpoint: str
    s3_bucket: str
    session_cookie_secret: str = Field(min_length=32)
    bootstrap_token: str = Field(min_length=32)
    session_ttl_seconds: int = Field(default=28_800, ge=300, le=604_800)

    # Execution tuning. Every bound is explicit so an operator cannot configure
    # a lease shorter than a slice, or a retention window that silently
    # discards events a live subscriber still needs.
    worker_lease_seconds: int = Field(default=30, ge=10, le=300)
    worker_max_slice_seconds: int = Field(default=30, ge=10, le=300)
    worker_idle_poll_seconds: int = Field(default=2, ge=1, le=30)
    worker_shutdown_grace_seconds: int = Field(default=20, ge=5, le=120)
    scheduler_interval_seconds: int = Field(default=5, ge=1, le=60)
    max_recovery_attempts: int = Field(default=3, ge=0, le=10)
    event_retention_hours: int = Field(default=168, ge=1, le=8_760)
    sse_heartbeat_seconds: int = Field(default=15, ge=5, le=60)
    deterministic_model_delay_ms: int = Field(default=50, ge=0, le=5_000)

    @field_validator("session_cookie_secret", "bootstrap_token")
    @classmethod
    def reject_example_secrets(cls, value: str) -> str:
        if value in {"change-me", "example", "secret"}:
            raise ValueError("example secrets are not valid runtime secrets")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]
