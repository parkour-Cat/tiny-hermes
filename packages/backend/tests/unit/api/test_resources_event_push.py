from typing import Any

from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.runs.infrastructure.event_push import ANNOUNCE
from tiny_hermes.shared.config import Settings


def _settings(redis_url: str) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://app:app@127.0.0.1:1/app",
        "redis_url": redis_url,
        "s3_endpoint": "http://127.0.0.1:1",
        "s3_bucket": "tiny-hermes",
        "s3_access_key": "test-access-key",
        "s3_secret_key": "test-secret-key",
        "session_cookie_secret": "cookie-secret-with-at-least-32-characters",
        "bootstrap_token": "bootstrap-token-with-at-least-32-characters",
    }
    return Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_the_api_announces_the_events_it_writes() -> None:
    """Submitting, cancelling and steering a Run all write its events here."""
    factory = ApplicationResources(_settings("redis://127.0.0.1:1/0")).session_factory()

    assert ANNOUNCE in factory.kw["info"]


def test_without_redis_the_api_announces_nothing() -> None:
    factory = ApplicationResources(_settings("")).session_factory()

    assert ANNOUNCE not in factory.kw.get("info", {})
