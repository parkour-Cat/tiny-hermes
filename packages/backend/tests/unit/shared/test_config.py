from typing import Any

import pytest
from pydantic import ValidationError
from tiny_hermes.shared.config import Settings


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://app:app@db/app",
        "redis_url": "redis://redis:6379/0",
        "s3_endpoint": "http://minio:9000",
        "s3_bucket": "tiny-hermes",
        "session_cookie_secret": "cookie-secret-with-at-least-32-characters",
        "bootstrap_token": "bootstrap-token-with-at-least-32-characters",
    }
    values.update(overrides)
    return Settings(**values)


EXECUTION_BOUNDS = [
    ("worker_lease_seconds", 30, 10, 300),
    ("worker_max_slice_seconds", 30, 10, 300),
    ("worker_idle_poll_seconds", 2, 1, 30),
    ("worker_shutdown_grace_seconds", 20, 5, 120),
    ("scheduler_interval_seconds", 5, 1, 60),
    ("max_recovery_attempts", 3, 0, 10),
    ("event_retention_hours", 168, 1, 8760),
    ("sse_heartbeat_seconds", 15, 5, 60),
    ("deterministic_model_delay_ms", 50, 0, 5000),
]


def test_settings_reject_placeholder_cookie_secret() -> None:
    with pytest.raises(ValidationError):
        _settings(session_cookie_secret="change-me")


@pytest.mark.parametrize(("name", "default", "low", "high"), EXECUTION_BOUNDS)
def test_execution_settings_default_inside_their_documented_range(
    name: str, default: int, low: int, high: int
) -> None:
    assert getattr(_settings(), name) == default
    assert low <= default <= high


@pytest.mark.parametrize(("name", "default", "low", "high"), EXECUTION_BOUNDS)
def test_execution_settings_accept_their_boundaries(
    name: str, default: int, low: int, high: int
) -> None:
    del default
    assert getattr(_settings(**{name: low}), name) == low
    assert getattr(_settings(**{name: high}), name) == high


@pytest.mark.parametrize(("name", "default", "low", "high"), EXECUTION_BOUNDS)
def test_execution_settings_reject_values_outside_their_range(
    name: str, default: int, low: int, high: int
) -> None:
    del default
    with pytest.raises(ValidationError):
        _settings(**{name: low - 1})
    with pytest.raises(ValidationError):
        _settings(**{name: high + 1})
