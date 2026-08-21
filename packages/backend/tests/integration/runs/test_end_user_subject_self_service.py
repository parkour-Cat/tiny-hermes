"""§4.6's "本人" row for an end user: export, and erasure that actually erases.

The roadmap's exit criterion for this task, its own two verbs: 小张 can
export his own data, and after erasure it is not retrievable. `SubjectService`
itself is exercised exhaustively elsewhere (`test_subject_erasure.py`, the
platform-member path); this suite is only about the second door
(`memory/presentation/end_user_subject_routes.py`) reaching the same service
for a caller who was never a workspace member.
"""

import json
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


def _published_agent(client: TestClient, scope: dict[str, str], alias: str) -> str:
    """A second end-user-enabled Agent, alongside `published_end_user_agent`
    — same recipe, different alias, so a subject can have memory under two
    Agents rather than the one the fixture already covers."""
    spec = {**VALID_SPEC, "end_user_access": {"enabled": True}}
    created = client.post(
        "/api/v1/agents", headers=scope, json={"name": alias.title(), "alias": alias}
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


async def test_an_end_user_can_export_their_own_memory(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    registered_issuer: dict[str, object],
    published_end_user_agent: str,
) -> None:
    """Finding 3: the shipped call (`SettingsPage.tsx`'s `exportData`) never
    sends `agent_id` — there is no Agent picker in the settings page, and no
    reason for one, since a subject's export is supposed to be *their* data,
    not one Agent's slice of it. This calls the door exactly as the UI does
    — no `agent_id` — and expects memory from two different Agents back in
    one answer, which is what "across every Agent they have used" means."""
    del registered_issuer
    second_agent = _published_agent(client, scope, "concierge")
    end_user_id = _sign_in(client, workspace_id, "zhang")
    await _remember(
        engine,
        workspace_id=workspace_id,
        agent_id=published_end_user_agent,
        body="Prefers tea over coffee.",
        end_user_id=end_user_id,
    )
    await _remember(
        engine,
        workspace_id=workspace_id,
        agent_id=second_agent,
        body="Travels for work twice a month.",
        end_user_id=end_user_id,
    )

    exported = client.get("/api/v1/end-user/subjects/me/export")

    assert exported.status_code == 200, exported.text
    body = exported.json()
    assert body["subject_id"] == str(end_user_id)
    assert body["subject_type"] == "end_user"
    bodies = {m["body"] for m in body["memories"]}
    assert "Prefers tea over coffee." in bodies
    assert "Travels for work twice a month." in bodies


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

    # Not a second export: finding 2 means the cookie that just erased itself
    # is revoked in the same call, so reusing it here would prove nothing
    # about the memory — it would just prove the cookie is dead (which the
    # self-service suite's own erasure test already covers). What "removed"
    # means for the memory is asked of the table directly.
    async with engine.connect() as connection:
        remaining = await connection.scalar(
            text("SELECT count(*) FROM memories WHERE subject_id = :id"),
            {"id": end_user_id},
        )
    assert remaining == 0


async def test_erasure_reaches_the_identifying_row_the_session_and_a_returning_credential(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    registered_issuer: dict[str, object],
) -> None:
    """Finding 2: a "successful" erasure that leaves `end_users.erased_at`
    unset, `external_identities` carrying whatever identifying detail a
    credential put there, and the cookie the erasing end user is *still
    holding* live, is not an erasure at all — it is a count that ran.

    The decision this pins: an erased subject who comes back with the same
    enterprise credential is **refused**, not handed a fresh `EndUser`. That
    is only possible because the `external_identities` row survives with its
    `(channel, external_user_id)` mapping intact — `upsert_external_identity`
    has to find *this* end user again, now carrying `erased_at`, for
    `EndUserIdentityService.exchange`'s guard to have anything to refuse.
    `profile` is what actually gets cleared: design §3's own words are that
    it is "the row ... that can be cleared without touching the subject the
    rest of the platform points at" — the mapping stays, the identifying
    fields on it do not.
    """
    del scope
    end_user_id = _sign_in(client, workspace_id, "zhang")
    # No producer writes `profile` yet (nothing in this credential shape
    # does), so this stands in for the day one does — the erasure has to
    # clear it regardless of who fills it in.
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE external_identities SET profile = :p WHERE end_user_id = :id"),
            {"p": json.dumps({"name": "Zhang Wei"}), "id": end_user_id},
        )

    erased = client.post("/api/v1/end-user/subjects/me/erase")
    assert erased.status_code == 200, erased.text

    async with engine.connect() as connection:
        erased_at = await connection.scalar(
            text("SELECT erased_at FROM end_users WHERE id = :id"), {"id": end_user_id}
        )
        profile = await connection.scalar(
            text("SELECT profile FROM external_identities WHERE end_user_id = :id"),
            {"id": end_user_id},
        )
        live_sessions = await connection.scalar(
            text(
                "SELECT count(*) FROM end_user_sessions "
                "WHERE end_user_id = :id AND revoked_at IS NULL"
            ),
            {"id": end_user_id},
        )
    assert erased_at is not None
    assert profile is None
    assert live_sessions == 0

    # The cookie the erasing end user is still holding no longer works.
    still_holding_the_cookie = client.get("/api/v1/end-user/subjects/me/export")
    assert still_holding_the_cookie.status_code == 401

    # And the same enterprise `sub` is refused, not resurrected as a new
    # `EndUser` — the same generic refusal every other credential problem
    # gets (design §8), never a distinguishing error.
    resurrection = client.post(
        "/api/v1/end-user/sessions",
        headers={
            "Authorization": f"Bearer {_credential(workspace_id=workspace_id, sub='zhang')}",
            "X-Workspace-Id": workspace_id,
        },
    )
    assert resurrection.status_code == 401
    assert resurrection.json()["code"] == "end_user_credential_invalid"
