"""The metrics port is a convenience to the operator, not a precondition."""

import logging
import socket
from typing import Any

import pytest
from tiny_hermes.shared.config import Settings
from tiny_hermes.shared.metrics import serve_configured_metrics


def _settings(port: int) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://app:app@db/app",
        "redis_url": "redis://redis:6379/0",
        "s3_endpoint": "http://minio:9000",
        "s3_bucket": "tiny-hermes",
        "s3_access_key": "test-access-key",
        "s3_secret_key": "test-secret-key",
        "session_cookie_secret": "cookie-secret-with-at-least-32-characters",
        "bootstrap_token": "bootstrap-token-with-at-least-32-characters",
        "metrics_host": "127.0.0.1",
        "metrics_port": port,
    }
    return Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_a_taken_port_is_logged_and_the_process_goes_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A Worker that cannot publish metrics can still run Runs; one that
    exits over it runs none, and the scrape target being down says enough."""
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        with caplog.at_level(logging.ERROR):
            serve_configured_metrics(_settings(port))

    assert any("metrics not served" in record.getMessage() for record in caplog.records)


def test_a_process_that_needs_a_token_serves_nothing_without_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        serve_configured_metrics(_settings(1), needs_token=True)

    assert any("METRICS_TOKEN" in record.getMessage() for record in caplog.records)
