"""Task-9 review finding A: entitlement was frozen for the session's own TTL
(up to 8 hours) while design §4.1 promises a 15-minute worst case.

`end_user_sessions.agents` is a snapshot of the credential's own `agents`
claim, taken once at exchange time (`identity/infrastructure/
end_user_session_tables.py`), and `create_run` used to submit against it
with no re-check at all — the two-gate evaluation
(`AgentCatalog.resolve_end_user_agent`) only ever ran inside `create_session`.
Closing `end_user_access.enabled` or letting an Agent's last published
version go away therefore had no effect on a Session that already held a
cookie, for as long as its own TTL allowed it to keep submitting.

This suite proves the fix at the HTTP layer `create_run` is actually reached
through: the platform gate is re-evaluated on every submission, not only at
Session creation, so revoking *our own* data takes effect on the very next
Run — and a control test that an untouched entitlement is unaffected by any
of this.
"""

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx2 as httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.api.app import create_app
from tiny_hermes.identity.presentation.end_user_dependencies import END_USER_SESSION_COOKIE
from tiny_hermes.shared.config import Settings

from ..conftest import VALID_SPEC

ISSUER = "https://idp.acme.example"
CHANNEL = "web"
ALIAS = "recheck-bot"


def _rsa_keypair() -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


RSA_PRIVATE_PEM, RSA_PUBLIC_PEM = _rsa_keypair()


def _credential(*, workspace_id: str, sub: str) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": ISSUER,
        "sub": sub,
        "aud": workspace_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
        "agents": [ALIAS],
    }
    return jwt.encode(payload, RSA_PRIVATE_PEM, algorithm="RS256")


@pytest.fixture
def client(empty_database: None, settings: Settings) -> Iterator[TestClient]:
    """Same override every end-user suite makes: the session cookie is
    `Secure` unconditionally, and `httpx`'s jar only attaches a `Secure`
    cookie to an `https://` base URL."""
    del empty_database
    with TestClient(create_app(settings=settings), base_url="https://testserver") as value:
        yield value


@pytest.fixture
def registered_issuer(client: TestClient, scope: dict[str, str]) -> None:
    created = client.post(
        "/api/v1/channel-issuers",
        headers=scope,
        json={
            "channel": CHANNEL,
            "issuer": ISSUER,
            "public_key": RSA_PUBLIC_PEM,
            "allowed_origins": ["https://acme.example"],
        },
    )
    assert created.status_code == 201, created.text


@contextmanager
def _as_admin(client: TestClient) -> Generator[None]:
    """Every console call in this suite happens *after* `_sign_in`, unlike
    every other end-user suite's fixture ordering — this one is specifically
    about an admin acting on a Session that already exists. One browser
    cannot hold both identities' cookies at once against a console door
    (Finding G's own subject: `reject_end_user_caller` refuses any console
    request carrying the end-user cookie, full stop), so the end-user cookie
    is set aside for the admin call and put back after — standing in for
    what is, in reality, the admin's own separate browser."""
    saved = client.cookies.get(END_USER_SESSION_COOKIE)
    if saved is not None:
        del client.cookies[END_USER_SESSION_COOKIE]
    try:
        yield
    finally:
        if saved is not None:
            client.cookies.set(END_USER_SESSION_COOKIE, saved)


def _publish(client: TestClient, scope: dict[str, str], *, enabled: bool) -> str:
    """Publishes (or republishes) `ALIAS` with `end_user_access.enabled` set
    as asked. Returns the Agent's id so a test can reach further into its
    draft/publish cycle, or manipulate its row directly for the "unpublished
    entirely" case no endpoint reaches."""
    spec = {**VALID_SPEC, "end_user_access": {"enabled": enabled}}
    existing = client.get("/api/v1/agents", headers=scope)
    assert existing.status_code == 200, existing.text
    matches = [a for a in existing.json() if a["alias"] == ALIAS]
    if matches:
        agent_id = matches[0]["id"]
        draft = client.get(f"/api/v1/agents/{agent_id}/draft", headers=scope)
        assert draft.status_code == 200, draft.text
        expected_revision = draft.json()["revision"]
    else:
        created = client.post(
            "/api/v1/agents", headers=scope, json={"name": "Recheck Bot", "alias": ALIAS}
        )
        assert created.status_code == 201, created.text
        agent_id = created.json()["id"]
        expected_revision = 1
    draft = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": expected_revision, "spec": spec},
    )
    assert draft.status_code == 200, draft.text
    published = client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=scope,
        json={"expected_revision": draft.json()["revision"]},
    )
    assert published.status_code == 201, published.text
    return str(agent_id)


def _sign_in(client: TestClient, workspace_id: str, sub: str) -> UUID:
    exchanged = client.post(
        "/api/v1/end-user/sessions",
        headers={
            "Authorization": f"Bearer {_credential(workspace_id=workspace_id, sub=sub)}",
            "X-Workspace-Id": workspace_id,
        },
    )
    assert exchanged.status_code == 201, exchanged.text
    assert exchanged.cookies.get(END_USER_SESSION_COOKIE) is not None
    return UUID(exchanged.json()["end_user_id"])


def _start_session(client: TestClient) -> str:
    created = client.post(f"/api/v1/end-user/agents/{ALIAS}/sessions", json={})
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def _submit(client: TestClient, session_id: str, key: str) -> httpx.Response:
    return client.post(
        f"/api/v1/end-user/sessions/{session_id}/runs",
        headers={"Idempotency-Key": key},
        json={"input": "hello"},
    )


async def test_closing_the_platform_gate_stops_the_next_submission_on_an_open_session(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
) -> None:
    del registered_issuer
    _publish(client, scope, enabled=True)
    _sign_in(client, workspace_id, "zhang")
    session_id = _start_session(client)

    first = _submit(client, session_id, "recheck-1")
    assert first.status_code == 201, first.text

    # The workspace admin closes §5's platform gate — the same republish an
    # admin would do through the console UI, no different from turning any
    # other draft field off and publishing again.
    with _as_admin(client):
        _publish(client, scope, enabled=False)

    second = _submit(client, session_id, "recheck-2")

    assert second.status_code == 403, second.text
    assert second.json()["code"] == "end_user_access_gate_closed"


async def test_unpublishing_the_agent_stops_the_next_submission_on_an_open_session(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    registered_issuer: None,
) -> None:
    """No endpoint takes an Agent's last published version away entirely —
    this platform never built one — but the row can still end up that way
    (an operational fix, a future admin action), and the re-check must not
    assume `current_version_id` can only ever move to another published
    version."""
    del registered_issuer
    agent_id = _publish(client, scope, enabled=True)
    _sign_in(client, workspace_id, "zhang")
    session_id = _start_session(client)

    first = _submit(client, session_id, "recheck-3")
    assert first.status_code == 201, first.text

    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE agents SET current_version_id = NULL WHERE id = :id"),
            {"id": UUID(agent_id)},
        )

    second = _submit(client, session_id, "recheck-4")

    assert second.status_code == 403, second.text


async def test_the_sessions_own_snapshot_no_longer_listing_the_agent_stops_submission(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    registered_issuer: None,
) -> None:
    """The enterprise half of the two gates: the credential's own `agents`
    claim is gone the moment the exchange finished, so
    `end_user_sessions.agents` is the only record left of what it said.
    Simulating a session that no longer lists this Agent — the shape a
    narrower re-issued credential's *next* exchange would produce — proves
    the re-check reads this Session's own snapshot again on submission
    rather than trusting the grant `create_session` made once."""
    del registered_issuer
    _publish(client, scope, enabled=True)
    end_user_id = _sign_in(client, workspace_id, "zhang")
    session_id = _start_session(client)

    first = _submit(client, session_id, "recheck-5")
    assert first.status_code == 201, first.text

    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE end_user_sessions SET agents = '[]' "
                "WHERE end_user_id = :end_user_id"
            ),
            {"end_user_id": end_user_id},
        )

    second = _submit(client, session_id, "recheck-6")

    assert second.status_code == 403, second.text
    assert second.json()["code"] == "end_user_agent_not_found"


async def test_an_untouched_entitlement_keeps_submitting_fine(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
) -> None:
    """Control: the re-check must not turn into a tax on the ordinary case.
    Nothing about this Session's entitlement changes between these two
    submissions, so both must succeed exactly as they did before this task."""
    del registered_issuer
    _publish(client, scope, enabled=True)
    _sign_in(client, workspace_id, "zhang")
    session_id = _start_session(client)

    first = _submit(client, session_id, "recheck-7")
    assert first.status_code == 201, first.text

    second = _submit(client, session_id, "recheck-8")
    assert second.status_code == 201, second.text
