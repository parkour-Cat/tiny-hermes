"""The first exit criterion: A's memory never reaches B.

§14.1 isolates private memory by workspace, agent and subject. That is decided
in the domain and enforced by four columns, but neither of those is where it
would fail — it would fail in the read path that assembles a round, where one
missing filter looks like nothing at all.

So this suite asserts it where a person would eventually notice: in the bytes
sent to the model. Two subjects, one Agent, one memory each, and the request
for one contains its own line and not the other's.

The memories are written straight to the table. There is no write path yet —
that is the next step of the plan — and the point of this step is that reading
is already safe before anything can write.
"""

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.ports.model import ModelRequest, ModelResponse

from ..conftest import VALID_SPEC


class Recorder:
    """The stand-in provider, with every request it answered kept.

    "The memory is in the request" is a statement about the bytes sent to the
    model, and this is the only place that can see them.
    """

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


def _agent(client: TestClient, scope: dict[str, str], alias: str = "remem") -> str:
    created = client.post(
        "/api/v1/agents", headers=scope, json={"name": alias.title(), "alias": alias}
    )
    assert created.status_code == 201, created.text
    agent_id = str(created.json()["id"])
    draft = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": VALID_SPEC},
    )
    assert draft.status_code == 200, draft.text
    published = client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=scope,
        json={"expected_revision": draft.json()["revision"]},
    )
    assert published.status_code == 201, published.text
    return agent_id


async def _remember(
    engine: AsyncEngine,
    *,
    workspace_id: str,
    agent_id: str,
    body: str,
    subject_id: UUID | None,
    status: str = "active",
) -> None:
    """One memory, written straight to the table.

    `subject_id` of `None` writes the Agent's shared memory — the one row shape
    where having no owner is correct.
    """
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO memories (id, workspace_id, agent_id, kind, "
                "subject_type, subject_id, body, status, origin, context, "
                "created_at, updated_at) VALUES (:id, :workspace, :agent, :kind, "
                ":subject_type, :subject, :body, :status, 'operator', '{}', "
                "now(), now())"
            ),
            {
                "id": uuid4(),
                "workspace": UUID(workspace_id),
                "agent": UUID(agent_id),
                "kind": "private" if subject_id is not None else "shared",
                "subject_type": "user" if subject_id is not None else None,
                "subject": subject_id,
                "body": body,
                "status": status,
            },
        )


async def _session_subject(engine: AsyncEngine, session_id: str) -> UUID:
    """Who the platform thinks started this Session.

    Read rather than assumed: the whole isolation rule turns on this being the
    same identity the memory was filed under, and a test that assumed it would
    pass while the two drifted apart.
    """
    async with engine.connect() as connection:
        found = await connection.execute(
            text("SELECT caller_id FROM sessions WHERE id = :id"),
            {"id": UUID(session_id)},
        )
        return UUID(str(found.scalar()))


def _run(client: TestClient, scope: dict[str, str], session_id: str) -> dict[str, Any]:
    created = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": f"memory-{session_id}"},
        json={"session_id": session_id, "input": "go"},
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


@pytest.fixture
def agent_id(client: TestClient, scope: dict[str, str]) -> str:
    return _agent(client, scope)


async def test_a_subject_is_told_their_own_memory(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    agent_id: str,
) -> None:
    session_id = session_for(agent_id)
    subject = await _session_subject(engine, session_id)
    await _remember(
        engine,
        workspace_id=scope["X-Workspace-Id"],
        agent_id=agent_id,
        body="Prefers the summary before the detail.",
        subject_id=subject,
    )
    _run(client, scope, session_id)
    model = Recorder()

    await _worker(engine, scope["X-Workspace-Id"], model).run_once()

    assert model.requests
    assert "Prefers the summary before the detail." in model.requests[0].memories


async def test_another_subject_s_memory_is_not_in_the_request(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    agent_id: str,
) -> None:
    """The exit criterion, asserted in the bytes.

    One Agent, two subjects. The Run belongs to one of them and the memory to
    the other, and nothing about the Agent, the workspace or the Session is
    different between them — which is exactly the case a missing filter would
    let through.
    """
    session_id = session_for(agent_id)
    somebody_else = uuid4()
    await _remember(
        engine,
        workspace_id=scope["X-Workspace-Id"],
        agent_id=agent_id,
        body="Their salary is confidential.",
        subject_id=somebody_else,
    )
    _run(client, scope, session_id)
    model = Recorder()

    await _worker(engine, scope["X-Workspace-Id"], model).run_once()

    assert model.requests
    assert model.requests[0].memories == ()


async def test_a_pending_memory_never_reaches_the_model(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    agent_id: str,
) -> None:
    """The difference between proposing and remembering. A candidate in the
    context would have been remembered without anybody agreeing, which is what
    §14.1's policies exist to decide."""
    session_id = session_for(agent_id)
    subject = await _session_subject(engine, session_id)
    await _remember(
        engine,
        workspace_id=scope["X-Workspace-Id"],
        agent_id=agent_id,
        body="Waiting for somebody to look at this.",
        subject_id=subject,
        status="pending",
    )
    _run(client, scope, session_id)
    model = Recorder()

    await _worker(engine, scope["X-Workspace-Id"], model).run_once()

    assert model.requests[0].memories == ()


async def test_the_agent_s_shared_memory_reaches_every_subject(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    agent_id: str,
) -> None:
    """The other half of the rule. Shared memory belongs to the Agent, so it is
    read by whoever is talking to it — that is what makes §14.2 insist it can
    only come from an administrator or an approved proposal."""
    session_id = session_for(agent_id)
    await _remember(
        engine,
        workspace_id=scope["X-Workspace-Id"],
        agent_id=agent_id,
        body="This company ships on Thursdays.",
        subject_id=None,
    )
    _run(client, scope, session_id)
    model = Recorder()

    await _worker(engine, scope["X-Workspace-Id"], model).run_once()

    assert "This company ships on Thursdays." in model.requests[0].memories


async def test_another_agent_s_memory_is_not_in_the_request(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    agent_id: str,
) -> None:
    """§14.1 keys on the Agent as well as the subject: what somebody told one
    Agent is not what they told another."""
    session_id = session_for(agent_id)
    subject = await _session_subject(engine, session_id)
    other_agent = _agent(client, scope, alias="elsewhere")
    await _remember(
        engine,
        workspace_id=scope["X-Workspace-Id"],
        agent_id=other_agent,
        body="Told to a different Agent entirely.",
        subject_id=subject,
    )
    _run(client, scope, session_id)
    model = Recorder()

    await _worker(engine, scope["X-Workspace-Id"], model).run_once()

    assert model.requests[0].memories == ()
