"""Design §6 / brief §6: a developer may read an end user's transcript, and
the price of that permission is one audit row.

Two things this suite pins down, because §6 is the only place in the
end-user-entry design that changes behaviour that already existed:

- Reading message *content* through the console (`GET .../sessions/{id}
  /messages`) writes `end_user_session.read`, naming both the reader and
  the end user whose words they read. `list_sessions` (titles only, no
  content) writes nothing — a page of forty sessions must not turn into
  forty audit rows.
- The three verbs §4.6 did **not** open to a developer — correct, forget,
  erase — stay refused. `subject_service._require_self_or_steward` already
  refuses anyone who is not the subject or a workspace-admin steward; this
  is the test that pins that refusal against an end user's own memory
  rather than assuming it holds.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

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
ALIAS = "remem"
PASSWORD = "long-pass-123"  # noqa: S105 - fixed local test credential
READ_ACTION = "end_user_session.read"


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
def client(empty_database: None, settings: Settings):  # noqa: ANN201
    """Overridden for `https://testserver`: the end-user session cookie is
    `Secure` unconditionally (design §4.2), and `httpx`'s cookie jar will not
    attach a `Secure` cookie to a plain `http://` request."""
    del empty_database
    with TestClient(create_app(settings=settings), base_url="https://testserver") as value:
        yield value


@pytest.fixture
def registered_issuer(client: TestClient, scope: dict[str, str]) -> dict[str, object]:
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
    return dict(created.json())


@pytest.fixture
def published_end_user_agent(client: TestClient, scope: dict[str, str]) -> str:
    spec = {**VALID_SPEC, "end_user_access": {"enabled": True}}
    created = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Remem", "alias": ALIAS}
    )
    assert created.status_code == 201, created.text
    agent_id = str(created.json()["id"])
    draft = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": spec},
    )
    assert draft.status_code == 200, draft.text
    published = client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=scope,
        json={"expected_revision": draft.json()["revision"]},
    )
    assert published.status_code == 201, published.text
    return agent_id


def _sign_in_end_user(client: TestClient, workspace_id: str, sub: str) -> UUID:
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


def _start_end_user_session(client: TestClient) -> str:
    created = client.post(f"/api/v1/end-user/agents/{ALIAS}/sessions", json={})
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def _submit_end_user_run(client: TestClient, session_id: str, key: str) -> None:
    created = client.post(
        f"/api/v1/end-user/sessions/{session_id}/runs",
        headers={"Idempotency-Key": key},
        json={"input": "hello there"},
    )
    assert created.status_code == 201, created.text


async def _seed_user(engine: AsyncEngine, display_name: str, subject: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users (id, status, display_name, is_platform_admin, created_at) "
                "VALUES (gen_random_uuid(), 'active', :name, false, now())"
            ),
            {"name": display_name},
        )
        await connection.execute(
            text(
                "INSERT INTO auth_identities "
                "(id, user_id, provider, subject, password_hash, created_at) "
                "SELECT gen_random_uuid(), id, 'local', :subject, "
                "  (SELECT password_hash FROM auth_identities LIMIT 1), now() "
                "FROM users WHERE display_name = :name"
            ),
            {"subject": subject, "name": display_name},
        )


async def _invite_and_login_developer(
    client: TestClient, scope: dict[str, str], workspace_id: str, engine: AsyncEngine
) -> tuple[str, str]:
    """Returns `(developer_user_id, developer_csrf_token)`.

    Every console router refuses outright whenever the end-user session
    cookie is present at all (`reject_end_user_caller`, "no exceptions") —
    so the jar has to lose that cookie before the admin can invite anyone,
    even though the admin's own cookie is still sitting right next to it.
    Logging the developer in then replaces the admin's session cookie in the
    shared jar — the same trade `test_workspace_members.py` makes — so every
    call after this one is the developer's, not the admin's.
    """
    await _seed_user(engine, "Dev", "dev@example.com")
    if END_USER_SESSION_COOKIE in client.cookies:
        del client.cookies[END_USER_SESSION_COOKIE]
    invited = client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=scope,
        json={"email": "dev@example.com", "role": "developer"},
    )
    assert invited.status_code == 201, invited.text
    developer_id = str(invited.json()["user_id"])

    client.cookies.clear()
    login = client.post(
        "/api/v1/auth/sessions",
        json={"subject": "dev@example.com", "password": PASSWORD},
    )
    assert login.status_code == 201, login.text
    return developer_id, login.cookies["tiny_hermes_csrf"]


async def _memory_for_end_user(
    engine: AsyncEngine,
    *,
    workspace_id: str,
    agent_id: str,
    end_user_id: UUID,
    body: str,
) -> UUID:
    memory_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO memories (id, workspace_id, agent_id, kind, "
                "subject_type, subject_id, body, status, origin, context, "
                "created_at, updated_at) VALUES (:id, :workspace, :agent, "
                "'private', 'end_user', :subject, :body, 'active', 'operator', "
                "'{}', now(), now())"
            ),
            {
                "id": memory_id,
                "workspace": UUID(workspace_id),
                "agent": UUID(agent_id),
                "subject": end_user_id,
                "body": body,
            },
        )
    return memory_id


async def _read_audit_rows(engine: AsyncEngine, action: str) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT workspace_id, actor_id, resource_id, context, created_at "
                "FROM audit_events WHERE action = :a"
            ),
            {"a": action},
        )
        return [dict(row) for row in rows.mappings()]


async def test_developer_reads_end_user_session_content_and_it_is_audited(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    registered_issuer: dict[str, object],
    published_end_user_agent: str,
) -> None:
    del registered_issuer
    end_user_id = _sign_in_end_user(client, workspace_id, "zhang")
    session_id = _start_end_user_session(client)
    _submit_end_user_run(client, session_id, "read-1")

    developer_id, dev_csrf = await _invite_and_login_developer(client, scope, workspace_id, engine)
    dev_headers = {"X-Workspace-Id": workspace_id, "X-CSRF-Token": dev_csrf}

    read = client.get(f"/api/v1/sessions/{session_id}/messages", headers=dev_headers)
    assert read.status_code == 200, read.text
    assert read.json()[0]["parts"][0]["text"] == "hello there"

    rows = await _read_audit_rows(engine, READ_ACTION)
    assert len(rows) == 1
    row = rows[0]
    assert row["workspace_id"] == UUID(workspace_id)
    assert row["actor_id"] == UUID(developer_id)
    assert row["resource_id"] == UUID(session_id)
    assert row["context"]["end_user_id"] == str(end_user_id)
    assert row["created_at"] is not None


async def test_listing_sessions_writes_no_audit_row(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    registered_issuer: dict[str, object],
    published_end_user_agent: str,
) -> None:
    del registered_issuer
    _sign_in_end_user(client, workspace_id, "zhang")
    session_id = _start_end_user_session(client)
    _submit_end_user_run(client, session_id, "read-2")

    _developer_id, dev_csrf = await _invite_and_login_developer(client, scope, workspace_id, engine)
    dev_headers = {"X-Workspace-Id": workspace_id, "X-CSRF-Token": dev_csrf}

    listed = client.get("/api/v1/sessions", headers=dev_headers)
    assert listed.status_code == 200, listed.text
    assert any(item["id"] == session_id for item in listed.json())

    assert await _read_audit_rows(engine, READ_ACTION) == []

    # Reading the transcript afterwards still writes exactly one row: the
    # list above must not have consumed or blocked it.
    client.get(f"/api/v1/sessions/{session_id}/messages", headers=dev_headers)
    assert len(await _read_audit_rows(engine, READ_ACTION)) == 1


async def test_a_developer_cannot_correct_forget_or_erase_an_end_users_memory(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    registered_issuer: dict[str, object],
    published_end_user_agent: str,
) -> None:
    """§4.6 opened "查看" to a developer and nothing else. The refusal below
    is `SubjectService._require_self_or_steward` doing what it already did —
    this test exists so that stays true rather than assumed."""
    del registered_issuer
    end_user_id = _sign_in_end_user(client, workspace_id, "zhang")
    memory_id = await _memory_for_end_user(
        engine,
        workspace_id=workspace_id,
        agent_id=published_end_user_agent,
        end_user_id=end_user_id,
        body="Prefers the summary before the detail.",
    )

    _developer_id, dev_csrf = await _invite_and_login_developer(
        client, scope, workspace_id, engine
    )
    dev_headers = {"X-Workspace-Id": workspace_id, "X-CSRF-Token": dev_csrf}

    corrected = client.post(
        f"/api/v1/subjects/memories/{memory_id}/correct",
        headers=dev_headers,
        json={"body": "Prefers the detail before the summary."},
    )
    assert corrected.status_code == 403, corrected.text
    assert corrected.json()["code"] == "forbidden"

    forgotten = client.post(
        f"/api/v1/subjects/memories/{memory_id}/forget", headers=dev_headers
    )
    assert forgotten.status_code == 403, forgotten.text

    erased = client.post(
        f"/api/v1/subjects/{end_user_id}/erase", headers=dev_headers
    )
    assert erased.status_code == 403, erased.text
