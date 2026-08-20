"""§4.6's "本人" row for an end user: export, and erasure that actually erases.

The roadmap's exit criterion for this task, its own two verbs: 小张 can
export his own data, and after erasure it is not retrievable. `SubjectService`
itself is exercised exhaustively elsewhere (`test_subject_erasure.py`, the
platform-member path); this suite is only about the second door
(`memory/presentation/end_user_subject_routes.py`) reaching the same service
for a caller who was never a workspace member.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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


async def _remember(
    engine: AsyncEngine, *, workspace_id: str, agent_id: str, body: str, end_user_id: UUID
) -> None:
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
                "id": uuid4(),
                "workspace": UUID(workspace_id),
                "agent": UUID(agent_id),
                "subject": end_user_id,
                "body": body,
            },
        )


async def test_an_end_user_can_export_their_own_memory(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    registered_issuer: dict[str, object],
    published_end_user_agent: str,
) -> None:
    del registered_issuer
    end_user_id = _sign_in(client, workspace_id, "zhang")
    await _remember(
        engine,
        workspace_id=workspace_id,
        agent_id=published_end_user_agent,
        body="Prefers tea over coffee.",
        end_user_id=end_user_id,
    )

    exported = client.get(
        "/api/v1/end-user/subjects/me/export",
        params={"agent_id": published_end_user_agent},
    )

    assert exported.status_code == 200, exported.text
    body = exported.json()
    assert body["subject_id"] == str(end_user_id)
    assert body["subject_type"] == "end_user"
    assert any(m["body"] == "Prefers tea over coffee." for m in body["memories"])


async def test_erasure_removes_what_export_could_see(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    registered_issuer: dict[str, object],
    published_end_user_agent: str,
) -> None:
    del registered_issuer
    end_user_id = _sign_in(client, workspace_id, "zhang")
    await _remember(
        engine,
        workspace_id=workspace_id,
        agent_id=published_end_user_agent,
        body="Their salary is confidential.",
        end_user_id=end_user_id,
    )

    erased = client.post("/api/v1/end-user/subjects/me/erase")
    assert erased.status_code == 200, erased.text
    assert erased.json()["memories"] == 1

    exported = client.get(
        "/api/v1/end-user/subjects/me/export",
        params={"agent_id": published_end_user_agent},
    )
    assert exported.status_code == 200, exported.text
    assert exported.json()["memories"] == []
