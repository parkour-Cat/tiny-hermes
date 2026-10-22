"""Product design §23.16 (v2.19): local password login is throttled per account."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.api.app import create_app
from tiny_hermes.shared.config import Settings

from ..support import metric

ADMIN = "admin@example.com"
PASSWORD = "long-pass-123"  # noqa: S105 - a fixed local test value
LIMIT = 5


@pytest.fixture
async def api_client(engine: AsyncEngine, database_url: str) -> AsyncIterator[TestClient]:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE audit_events, auth_sessions, auth_identities, users, "
                "login_failures CASCADE"
            )
        )
    settings = Settings(
        database_url=database_url,
        redis_url="redis://localhost:6379/0",
        s3_endpoint="http://localhost:9000",
        s3_bucket="tiny-hermes",
        s3_access_key="test-access-key",
        s3_secret_key="test-secret-key",
        session_cookie_secret="test-cookie-secret-with-32-characters",
        bootstrap_token="a" * 32,
    )
    with TestClient(create_app(settings=settings)) as client:
        created = client.post(
            "/api/v1/bootstrap",
            headers={"X-Bootstrap-Token": "a" * 32},
            json={"subject": ADMIN, "display_name": "Admin", "password": PASSWORD},
        )
        assert created.status_code == 201
        yield client


def _login(client: TestClient, password: str, subject: str = ADMIN) -> Any:
    return client.post(
        "/api/v1/auth/sessions", json={"subject": subject, "password": password}
    )


def _fail(client: TestClient, times: int, subject: str = ADMIN) -> None:
    for _ in range(times):
        assert _login(client, "wrong-password", subject).status_code == 401


async def _audits(engine: AsyncEngine, action: str) -> int:
    async with engine.connect() as connection:
        return int(
            await connection.scalar(
                text("SELECT count(*) FROM audit_events WHERE action = :action"),
                {"action": action},
            )
            or 0
        )


async def test_after_five_failures_even_the_right_password_is_refused(
    api_client: TestClient, engine: AsyncEngine
) -> None:
    """Letting the right password through would tell a guesser it was right."""
    throttled = metric("tiny_hermes_login_attempts_total", outcome="throttled")
    _fail(api_client, LIMIT)

    refused = _login(api_client, PASSWORD)

    assert refused.status_code == 429
    assert refused.json()["code"] == "login_throttled"
    assert 0 < int(refused.headers["Retry-After"]) <= 15 * 60
    assert await _audits(engine, "identity.login_throttled") == 1
    assert metric("tiny_hermes_login_attempts_total", outcome="throttled") == throttled + 1


async def test_four_failures_still_let_the_right_password_in(api_client: TestClient) -> None:
    _fail(api_client, LIMIT - 1)

    assert _login(api_client, PASSWORD).status_code == 201


async def test_a_success_clears_the_failures(api_client: TestClient) -> None:
    _fail(api_client, LIMIT - 1)
    assert _login(api_client, PASSWORD).status_code == 201
    _fail(api_client, LIMIT - 1)

    assert _login(api_client, PASSWORD).status_code == 201


async def test_failures_older_than_the_window_no_longer_count(
    api_client: TestClient, engine: AsyncEngine
) -> None:
    _fail(api_client, LIMIT)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE login_failures SET failed_at = failed_at - interval '16 minutes'")
        )

    assert _login(api_client, PASSWORD).status_code == 201


async def test_one_account_s_failures_do_not_throttle_another(api_client: TestClient) -> None:
    _fail(api_client, LIMIT, subject="someone-else@example.com")

    assert _login(api_client, PASSWORD).status_code == 201


async def test_an_account_that_does_not_exist_is_throttled_the_same_way(
    api_client: TestClient,
) -> None:
    """A difference here would let the throttle answer whether an account exists."""
    nobody = "nobody@example.com"
    _fail(api_client, LIMIT, subject=nobody)

    refused = _login(api_client, "anything", subject=nobody)

    assert refused.status_code == 429
    assert refused.json()["code"] == "login_throttled"


async def test_the_account_name_is_counted_by_case_and_spacing_it_normalises_to(
    api_client: TestClient,
) -> None:
    _fail(api_client, LIMIT, subject="  Admin@Example.COM ")

    assert _login(api_client, PASSWORD).status_code == 429


async def test_no_account_name_is_stored_in_the_clear(
    api_client: TestClient, engine: AsyncEngine
) -> None:
    """People type passwords into the account field."""
    _fail(api_client, 1, subject="hunter2-typed-in-the-wrong-box")

    async with engine.connect() as connection:
        stored = await connection.scalar(
            text("SELECT string_agg(subject_key, ',') FROM login_failures")
        )
    assert stored
    assert "hunter2" not in stored
    assert "@" not in stored
