import pytest
from pydantic import ValidationError
from tiny_hermes.shared.config import Settings


def test_settings_reject_placeholder_cookie_secret() -> None:
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+asyncpg://app:app@db/app",
            redis_url="redis://redis:6379/0",
            s3_endpoint="http://minio:9000",
            s3_bucket="tiny-hermes",
            session_cookie_secret="change-me",
            bootstrap_token="bootstrap-token-with-at-least-32-characters",
        )
