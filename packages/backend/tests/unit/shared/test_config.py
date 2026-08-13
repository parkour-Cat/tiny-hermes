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
        "s3_access_key": "test-access-key",
        "s3_secret_key": "test-secret-key",
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


# Design §15's 3C defaults. `workspace_max_bytes` is a checkpoint quota — what
# may become a committed revision — never a physical host-disk limit.
WORKSPACE_BOUNDS = [
    ("workspace_max_bytes", 2_147_483_648, 1_048_576, 1_099_511_627_776),
    ("workspace_max_objects", 100_000, 100, 10_000_000),
    ("workspace_file_list_entries", 1_000, 10, 10_000),
    ("workspace_file_read_bytes", 1_048_576, 1_024, 104_857_600),
    ("workspace_file_write_bytes", 16_777_216, 1_024, 1_073_741_824),
    ("workspace_staging_ttl_seconds", 86_400, 3_600, 604_800),
    ("workspace_transfer_timeout_seconds", 300, 30, 1_800),
    ("controller_stream_frame_bytes", 1_048_576, 4_096, 1_048_576),
    ("controller_stream_credit_bytes", 8_388_608, 1_048_576, 67_108_864),
    ("controller_stream_idle_seconds", 30, 5, 300),
    ("artifact_max_bytes", 104_857_600, 1_048_576, 1_073_741_824),
    ("run_artifact_max_bytes", 524_288_000, 1_048_576, 10_737_418_240),
]


@pytest.mark.parametrize(("name", "default", "low", "high"), WORKSPACE_BOUNDS)
def test_workspace_settings_default_inside_their_documented_range(
    name: str, default: int, low: int, high: int
) -> None:
    assert getattr(_settings(), name) == default
    assert low <= default <= high


@pytest.mark.parametrize(("name", "default", "low", "high"), WORKSPACE_BOUNDS)
def test_workspace_settings_reject_values_outside_their_range(
    name: str, default: int, low: int, high: int
) -> None:
    del default
    with pytest.raises(ValidationError):
        _settings(**{name: low - 1})
    with pytest.raises(ValidationError):
        _settings(**{name: high + 1})


def test_a_transfer_timeout_above_thirty_minutes_is_refused() -> None:
    """Named in design §15: 300 by default, configurable up to 1,800."""
    assert _settings(workspace_transfer_timeout_seconds=1_800) is not None
    with pytest.raises(ValidationError):
        _settings(workspace_transfer_timeout_seconds=1_801)


@pytest.mark.parametrize("missing", ["s3_access_key", "s3_secret_key"])
def test_object_store_credentials_are_required(
    missing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3C writes real objects, so a deployment without credentials must not start."""
    monkeypatch.delenv(missing.upper(), raising=False)
    values: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://app:app@db/app",
        "redis_url": "redis://redis:6379/0",
        "s3_endpoint": "http://minio:9000",
        "s3_bucket": "tiny-hermes",
        "s3_access_key": "test-access-key",
        "s3_secret_key": "test-secret-key",
        "session_cookie_secret": "cookie-secret-with-at-least-32-characters",
        "bootstrap_token": "bootstrap-token-with-at-least-32-characters",
    }
    del values[missing]
    # `_env_file=None` keeps a developer's local .env from satisfying the
    # requirement this test exists to prove.
    values["_env_file"] = None
    with pytest.raises(ValidationError):
        Settings(**values)
