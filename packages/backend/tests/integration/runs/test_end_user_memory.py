"""M2D's exit criterion, reshaped for the third subject.

`test_memory_isolation.py` proved A's private memory never reaches B's Run,
asserted in the bytes a Recorder captured — not in a query result, because a
missing filter in the read path is exactly the failure a query-result
assertion would not catch. This suite runs the same shape end to end through
the door §5 actually built: a credential exchanged for a session cookie, an
Agent reached through `AgentCatalog.resolve_end_user_agent`'s two gates, a
Session whose `caller_type` is `end_user`, and a Run submitted against it.

`client` is redefined the way `test_end_user_sessions.py` redefines it: an
`https://testserver` base URL, because the end-user session cookie is
`Secure` unconditionally (design §4.2) and `httpx`'s cookie jar will not
attach a `Secure` cookie to a plain `http://` request. Two end users need two
separate cookie jars, so this suite opens a second `https://testserver`
client for the second one rather than reusing the module fixture, the same
way two browsers would.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.api.app import create_app
from tiny_hermes.identity.presentation.end_user_dependencies import END_USER_SESSION_COOKIE
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.deterministic_model import DeterministicModelProvider
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.ports.model import ModelRequest, ModelResponse
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


def _credential(*, workspace_id: str, sub: str, agents: list[str] | None = None) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": ISSUER,
        "sub": sub,
        "aud": workspace_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
        "agents": [ALIAS] if agents is None else agents,
    }
    return jwt.encode(payload, RSA_PRIVATE_PEM, algorithm="RS256")


class Recorder:
    """The stand-in provider, with every request it answered kept — the same
    device `test_memory_isolation.py` uses, because "the memory is in the
    request" is a statement about the bytes sent to the model and this is the
    only place that can see them."""

    def __init__(self) -> None:
        self.inner = DeterministicModelProvider(delay_ms=0)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await self.inner.complete(request)


def _worker(engine: AsyncEngine, workspace_id: str, model: Recorder) -> WorkerRuntime:
    return WorkerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        model=model,
        notifier=NullWakeUpNotifier(),
        settings=WorkerSettings(
            worker_id="worker-a",
            lease_seconds=30,
            max_slice_seconds=30,
            idle_poll_seconds=1,
            workspace_id=UUID(workspace_id),
        ),
    )


async def _remember(
    engine: AsyncEngine,
    *,
    workspace_id: str,
    agent_id: str,
    body: str,
    end_user_id: UUID,
) -> None:
    """One private memory for an end-user subject, written straight to the
    table — `test_memory_isolation.py` writes its own the same way, on the
    grounds that this suite is proving the *read* path, not the write path
    memory.remember exercises elsewhere."""
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


@pytest.fixture
def client(empty_database: None, settings: Settings) -> Iterator[TestClient]:
    """Overrides `tests/integration/conftest.py`'s `client`, the same way
    `test_end_user_sessions.py` does: `workspace_id`/`scope`/`admin_csrf`
    all resolve `client` by name, and the `https://testserver` base URL is
    what makes them usable with a cookie jar that will actually carry the
    `Secure` end-user cookie. `with` rather than a bare construction — a
    `TestClient` outside its own context manager has no event-loop portal,
    and this suite opens a *second* one for the other end user, which is
    exactly the shape that portal exists to keep straight."""
    del empty_database
    with TestClient(create_app(settings=settings), base_url="https://testserver") as value:
        yield value


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


def _submit(client: TestClient, session_id: str, key: str) -> dict[str, Any]:
    created = client.post(
        f"/api/v1/end-user/sessions/{session_id}/runs",
        headers={"Idempotency-Key": key},
        json={"input": "go"},
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


# -- the door itself: caller_type=end_user, all the way down ----------------


async def test_an_end_users_session_is_recorded_as_caller_type_end_user(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    registered_issuer: dict[str, object],
    published_end_user_agent: str,
) -> None:
    del registered_issuer, published_end_user_agent
    end_user_id = _sign_in(client, workspace_id, "zhang")
    session_id = _start_session(client)

    async with engine.connect() as connection:
        found = await connection.execute(
            text("SELECT caller_type, caller_id FROM sessions WHERE id = :id"),
            {"id": UUID(session_id)},
        )
        caller_type, caller_id = found.one()

    assert caller_type == "end_user"
    assert UUID(str(caller_id)) == end_user_id


async def test_the_run_confirms_to_the_real_end_user(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    registered_issuer: dict[str, object],
    published_end_user_agent: str,
) -> None:
    """§5: `runs.end_user_id` is the real `end_users.id`, not a stand-in —
    the fact `SqlApprovalGate._subject` leans on to decide who may answer a
    `user_confirmation`."""
    del registered_issuer, published_end_user_agent
    end_user_id = _sign_in(client, workspace_id, "zhang")
    session_id = _start_session(client)
    run = _submit(client, session_id, "run-1")

    async with engine.connect() as connection:
        found = await connection.execute(
            text("SELECT end_user_id FROM runs WHERE id = :id"), {"id": UUID(run["id"])}
        )
        assert UUID(str(found.scalar())) == end_user_id


# -- the exit criterion, asserted in the bytes -------------------------------


async def test_a_subject_is_told_their_own_memory(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    registered_issuer: dict[str, object],
    published_end_user_agent: str,
) -> None:
    del registered_issuer
    end_user_id = _sign_in(client, workspace_id, "zhang")
    session_id = _start_session(client)
    await _remember(
        engine,
        workspace_id=workspace_id,
        agent_id=published_end_user_agent,
        body="Prefers the summary before the detail.",
        end_user_id=end_user_id,
    )
    _submit(client, session_id, "run-a")
    model = Recorder()

    await _worker(engine, workspace_id, model).run_once()

    assert model.requests
    assert "Prefers the summary before the detail." in model.requests[0].memories


async def test_another_end_users_memory_is_not_in_the_request(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    settings: Settings,
    engine: AsyncEngine,
    registered_issuer: dict[str, object],
    published_end_user_agent: str,
) -> None:
    """The exit criterion. One Agent, two end users — 小张 and 小王 — and
    nothing about the Agent, the workspace, or the channel issuer differs
    between them, which is exactly the case a missing filter would let
    through."""
    del registered_issuer
    _sign_in(client, workspace_id, "zhang")
    zhang_session = _start_session(client)

    # A second cookie jar for 小王, the same way a second browser would carry
    # one — `client`'s own jar already holds 小张's cookie.
    with TestClient(create_app(settings=settings), base_url="https://testserver") as wang_client:
        wang_id = _sign_in(wang_client, workspace_id, "wang")
        await _remember(
            engine,
            workspace_id=workspace_id,
            agent_id=published_end_user_agent,
            body="Their salary is confidential.",
            end_user_id=wang_id,
        )

    _submit(client, zhang_session, "run-b")
    model = Recorder()

    await _worker(engine, workspace_id, model).run_once()

    assert model.requests
    assert "Their salary is confidential." not in model.requests[0].memories
