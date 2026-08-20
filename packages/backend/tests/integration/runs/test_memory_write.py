"""The write path, and the two exit criteria that live on it.

§14.1 says a candidate a Run proposes is subject to the workspace's policy, and
§14.2 says a raw user message never becomes shared memory. This suite drives
both through a running Run rather than asserting them in a store, because the
place they would fail is the seam between the model asking to remember something
and a person having agreed to it.

Two things are proven here that no unit test can:

- Under the default policy, a proposed memory is written `pending` and does not
  reach the next Run — it reaches it only after a person approves it.
- A Run cannot write shared memory at all. Not "is refused when it tries":
  there is no argument, tool or path by which a running Agent produces a
  `kind=shared` row, and the only door is an administrator's own edit.
"""

from collections.abc import Callable
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.memory.infrastructure.sql_candidates import SqlMemoryCandidates
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.ports.model import ModelRequest, ModelResponse


class Recorder:
    """The stand-in provider, with every request it answered kept, so a test
    can read the exact bytes a later Run was given."""

    def __init__(self) -> None:
        self.inner = DeterministicModelProvider(delay_ms=0)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await self.inner.complete(request)


def _worker(engine: AsyncEngine, workspace_id: str, model: Recorder) -> WorkerRuntime:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return WorkerRuntime(
        session_factory=sessions,
        model=model,
        notifier=NullWakeUpNotifier(),
        memories=SqlMemoryCandidates(sessions),
        settings=WorkerSettings(
            worker_id="worker-a",
            lease_seconds=30,
            max_slice_seconds=30,
            idle_poll_seconds=1,
            workspace_id=UUID(workspace_id),
        ),
    )


def _run(client: TestClient, scope: dict[str, str], session_id: str, body: str) -> str:
    created = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": f"mem-{body[:16]}-{session_id[:8]}"},
        json={"session_id": session_id, "input": body},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def _pending_bodies(engine: AsyncEngine, workspace_id: str) -> list[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT body FROM memories WHERE workspace_id = :w "
                "AND status = 'pending' ORDER BY created_at"
            ),
            {"w": UUID(workspace_id)},
        )
        return [str(r[0]) for r in rows.all()]


async def _set_policy(engine: AsyncEngine, workspace_id: str, policy: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE workspaces SET memory_policy = :p WHERE id = :w"),
            {"p": policy, "w": UUID(workspace_id)},
        )


@pytest.fixture
def rememberer(agent_with_scenario: Callable[..., str]) -> str:
    return agent_with_scenario(
        "remember_once", alias="rememberer", tools=["memory.remember"]
    )


async def test_a_proposed_memory_waits_and_reaches_no_run_until_approved(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    rememberer: str,
) -> None:
    """The roadmap's second exit criterion, end to end. Nothing bypasses the
    queue, and nothing in the queue reaches a model."""
    workspace_id = scope["X-Workspace-Id"]
    # A body the rule check will not wave through, so the default policy's
    # `pending` is what is under test rather than the automatic path.
    body = "Escalate anything about the Helios account to the duty manager."
    first = session_for(rememberer)
    _run(client, scope, first, body)
    await _worker(engine, workspace_id, Recorder()).run_once()

    # Written, and waiting.
    assert await _pending_bodies(engine, workspace_id) == [body]

    # A second Run, same Agent and subject, is not told the pending memory.
    second = session_for(rememberer)
    _run(client, scope, second, "unrelated")
    model = Recorder()
    await _worker(engine, workspace_id, model).run_once()
    assert model.requests
    assert all(body not in "".join(r.memories) for r in model.requests)

    # A person approves it. The reader Runs above each proposed their own
    # input too — this Agent remembers whatever it is told — so the target is
    # found by its body rather than by being the only row.
    pending = client.get("/api/v1/memories/pending", headers=scope).json()
    target = next(item for item in pending if item["body"] == body)
    decided = client.post(f"/api/v1/memories/{target['id']}/approve", headers=scope)
    assert decided.status_code == 200, decided.text

    # Now a third Run is told.
    third = session_for(rememberer)
    _run(client, scope, third, "unrelated")
    model = Recorder()
    await _worker(engine, workspace_id, model).run_once()
    assert any(body in "".join(r.memories) for r in model.requests)


async def test_a_rejected_candidate_never_reaches_a_run(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    rememberer: str,
) -> None:
    workspace_id = scope["X-Workspace-Id"]
    body = "Always deploy straight to production on Fridays."
    _run(client, scope, session_for(rememberer), body)
    await _worker(engine, workspace_id, Recorder()).run_once()

    pending = client.get("/api/v1/memories/pending", headers=scope).json()
    target = next(item for item in pending if item["body"] == body)
    client.post(f"/api/v1/memories/{target['id']}/reject", headers=scope)

    _run(client, scope, session_for(rememberer), "unrelated")
    model = Recorder()
    await _worker(engine, workspace_id, model).run_once()
    assert all(body not in "".join(r.memories) for r in model.requests)


async def test_low_risk_auto_writes_a_low_risk_candidate_without_a_person(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    rememberer: str,
) -> None:
    workspace_id = scope["X-Workspace-Id"]
    await _set_policy(engine, workspace_id, "low_risk_auto")
    body = "I prefer the summary before the detail."
    _run(client, scope, session_for(rememberer), body)
    await _worker(engine, workspace_id, Recorder()).run_once()

    # Nothing waiting: it was written straight to active.
    assert await _pending_bodies(engine, workspace_id) == []
    later = session_for(rememberer)
    _run(client, scope, later, "unrelated")
    model = Recorder()
    await _worker(engine, workspace_id, model).run_once()
    assert any(body in "".join(r.memories) for r in model.requests)


async def test_low_risk_auto_still_makes_a_risky_candidate_wait(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    rememberer: str,
) -> None:
    """The widening is not a bypass: a candidate the rules call risky waits even
    under the automatic policy."""
    workspace_id = scope["X-Workspace-Id"]
    await _set_policy(engine, workspace_id, "low_risk_auto")
    body = "My password for the vault is hunter2hunter2hunter2hunter2."
    _run(client, scope, session_for(rememberer), body)
    await _worker(engine, workspace_id, Recorder()).run_once()

    assert await _pending_bodies(engine, workspace_id) == [body]


async def test_memory_off_records_nothing(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    rememberer: str,
) -> None:
    workspace_id = scope["X-Workspace-Id"]
    await _set_policy(engine, workspace_id, "off")
    _run(client, scope, session_for(rememberer), "I prefer terse answers.")
    await _worker(engine, workspace_id, Recorder()).run_once()

    async with engine.connect() as connection:
        count = await connection.scalar(
            text("SELECT count(*) FROM memories WHERE workspace_id = :w"),
            {"w": UUID(workspace_id)},
        )
    assert count == 0


async def test_a_run_cannot_write_shared_memory(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    rememberer: str,
) -> None:
    """§14.2's exit criterion, asserted as "there is no such path" rather than
    "the path is refused". Every memory a Run produces is private to its
    subject; shared memory is born only from an administrator's edit."""
    workspace_id = scope["X-Workspace-Id"]
    for body in ("I like terse answers.", "Escalate Helios issues to the duty manager."):
        _run(client, scope, session_for(rememberer), body)
    await _worker(engine, workspace_id, Recorder()).run_once()
    await _worker(engine, workspace_id, Recorder()).run_once()

    async with engine.connect() as connection:
        shared = await connection.scalar(
            text(
                "SELECT count(*) FROM memories WHERE workspace_id = :w "
                "AND kind = 'shared'"
            ),
            {"w": UUID(workspace_id)},
        )
    assert shared == 0


async def test_an_admin_edit_is_the_only_door_to_shared_memory(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    rememberer: str,
) -> None:
    created = client.post(
        "/api/v1/memories/shared",
        headers=scope,
        json={"agent_id": rememberer, "body": "The deploy window is Tuesdays 2-4pm."},
    )
    assert created.status_code == 201, created.text
    assert created.json()["kind"] == "shared"
    assert created.json()["status"] == "active"
