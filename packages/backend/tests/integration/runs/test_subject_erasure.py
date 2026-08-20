"""What a subject may do with their own data, and what erasure really removes.

§4.6's "本人" row and the roadmap's sixth exit criterion. The criterion has two
halves and both are asserted here: after an erasure the subject's memories,
sessions, messages and files are **not retrievable by any path**, and the
erasure itself **wrote an audit record**.

The second half is the one worth the trouble. A deletion that left nothing
behind and a deletion that never ran look identical from the outside, so the
audit line is the only thing that tells them apart — and it carries counts
rather than content, because a record holding what it deleted would be the copy
the deletion was for.
"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.memory.infrastructure.run_searches import SqlRunSessionSearches
from tiny_hermes.memory.infrastructure.sql_candidates import SqlMemoryCandidates
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.ports.model import ModelRequest, ModelResponse


class Recorder:
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
        searches=SqlRunSessionSearches(sessions),
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
        headers={**scope, "Idempotency-Key": f"erase-{body[:14]}-{session_id[:8]}"},
        json={"session_id": session_id, "input": body},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def _subject_id(engine: AsyncEngine, session_id: str) -> UUID:
    async with engine.connect() as connection:
        found = await connection.execute(
            text("SELECT caller_id FROM sessions WHERE id = :s"),
            {"s": UUID(session_id)},
        )
        return UUID(str(found.scalar()))


async def _counts(engine: AsyncEngine, workspace_id: str, subject: UUID) -> dict[str, int]:
    async with engine.connect() as connection:
        memories = await connection.scalar(
            text(
                "SELECT count(*) FROM memories WHERE workspace_id = :w "
                "AND subject_id = :s"
            ),
            {"w": UUID(workspace_id), "s": subject},
        )
        sessions = await connection.scalar(
            text(
                "SELECT count(*) FROM sessions WHERE workspace_id = :w "
                "AND caller_id = :s"
            ),
            {"w": UUID(workspace_id), "s": subject},
        )
        messages = await connection.scalar(
            text(
                "SELECT count(*) FROM session_messages m JOIN sessions s "
                "ON s.id = m.session_id WHERE s.caller_id = :s"
            ),
            {"s": subject},
        )
    return {
        "memories": int(memories or 0),
        "sessions": int(sessions or 0),
        "messages": int(messages or 0),
    }


async def _audit(engine: AsyncEngine, action: str) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT context FROM audit_events WHERE action = :a"),
            {"a": action},
        )
        return [dict(r[0] or {}) for r in rows.all()]


@pytest.fixture
def rememberer(agent_with_scenario: Callable[..., str]) -> str:
    return agent_with_scenario(
        "remember_once", alias="eraser", tools=["memory.remember"]
    )


async def test_a_subject_exports_what_is_held_about_them(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    rememberer: str,
) -> None:
    workspace_id = scope["X-Workspace-Id"]
    session_id = session_for(rememberer)
    _run(client, scope, session_id, "I prefer the summary before the detail.")
    await _worker(engine, workspace_id, Recorder()).run_once()
    subject = await _subject_id(engine, session_id)

    exported = client.get(
        f"/api/v1/subjects/{subject}/export",
        headers=scope,
        params={"agent_id": rememberer},
    )

    assert exported.status_code == 200, exported.text
    body = exported.json()
    assert session_id in body["sessions"]
    assert any(
        "summary before the detail" in item["body"] for item in body["memories"]
    )


async def test_a_correction_keeps_what_the_memory_used_to_say(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    rememberer: str,
) -> None:
    """A corrected memory whose earlier text is gone is a record nobody can
    audit: "this was changed" and "this was always so" must stay apart."""
    workspace_id = scope["X-Workspace-Id"]
    session_id = session_for(rememberer)
    _run(client, scope, session_id, "I prefer the summary before the detail.")
    await _worker(engine, workspace_id, Recorder()).run_once()
    subject = await _subject_id(engine, session_id)
    exported = client.get(
        f"/api/v1/subjects/{subject}/export",
        headers=scope,
        params={"agent_id": rememberer},
    ).json()
    original = exported["memories"][0]

    corrected = client.post(
        f"/api/v1/subjects/memories/{original['id']}/correct",
        headers=scope,
        json={"body": "I prefer the detail before the summary."},
    )

    assert corrected.status_code == 200, corrected.text
    after = client.get(
        f"/api/v1/subjects/{subject}/export",
        headers=scope,
        params={"agent_id": rememberer},
    ).json()["memories"]
    bodies = {item["body"]: item["status"] for item in after}
    assert bodies["I prefer the summary before the detail."] == "rejected"
    assert bodies["I prefer the detail before the summary."] == "active"


async def test_a_forgotten_memory_stops_reaching_runs(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    rememberer: str,
) -> None:
    workspace_id = scope["X-Workspace-Id"]
    body = "I prefer the summary before the detail."
    session_id = session_for(rememberer)
    _run(client, scope, session_id, body)
    await _worker(engine, workspace_id, Recorder()).run_once()
    subject = await _subject_id(engine, session_id)
    memories = client.get(
        f"/api/v1/subjects/{subject}/export",
        headers=scope,
        params={"agent_id": rememberer},
    ).json()["memories"]
    target = next(item for item in memories if item["body"] == body)
    client.post(f"/api/v1/memories/{target['id']}/approve", headers=scope)

    forgotten = client.post(
        f"/api/v1/subjects/memories/{target['id']}/forget", headers=scope
    )
    assert forgotten.status_code == 200, forgotten.text

    _run(client, scope, session_for(rememberer), "unrelated")
    model = Recorder()
    await _worker(engine, workspace_id, model).run_once()
    assert all(body not in "".join(r.memories) for r in model.requests)


async def test_erasure_removes_everything_and_writes_an_audit_record(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    rememberer: str,
) -> None:
    """The sixth exit criterion, both halves."""
    workspace_id = scope["X-Workspace-Id"]
    session_id = session_for(rememberer)
    _run(client, scope, session_id, "I prefer the summary before the detail.")
    await _worker(engine, workspace_id, Recorder()).run_once()
    subject = await _subject_id(engine, session_id)
    before = await _counts(engine, workspace_id, subject)
    assert before["memories"] > 0
    assert before["sessions"] > 0
    assert before["messages"] > 0

    erased = client.post(f"/api/v1/subjects/{subject}/erase", headers=scope)

    assert erased.status_code == 200, erased.text
    # Nothing left, by any query.
    after = await _counts(engine, workspace_id, subject)
    assert after == {"memories": 0, "sessions": 0, "messages": 0}
    # And the erasure itself is on the record, in counts rather than content.
    records = await _audit(engine, "subject.erased")
    assert records
    assert int(records[-1]["memories"]) == before["memories"]
    assert "summary before the detail" not in str(records[-1])


async def test_an_erased_subject_is_told_nothing_rather_than_an_error(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    rememberer: str,
) -> None:
    """"There is nothing" is the honest answer; an error would read as
    "something went wrong" to somebody who just asked to be forgotten."""
    workspace_id = scope["X-Workspace-Id"]
    session_id = session_for(rememberer)
    _run(client, scope, session_id, "I prefer terse answers.")
    await _worker(engine, workspace_id, Recorder()).run_once()
    subject = await _subject_id(engine, session_id)
    client.post(f"/api/v1/subjects/{subject}/erase", headers=scope)

    exported = client.get(
        f"/api/v1/subjects/{subject}/export",
        headers=scope,
        params={"agent_id": rememberer},
    )

    assert exported.status_code == 200
    assert exported.json()["memories"] == []
    assert exported.json()["sessions"] == []


async def test_an_erased_subject_s_words_are_no_longer_searchable(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    rememberer: str,
) -> None:
    workspace_id = scope["X-Workspace-Id"]
    session_id = session_for(rememberer)
    _run(client, scope, session_id, "the flamingo release ships on Thursday")
    await _worker(engine, workspace_id, Recorder()).run_once()
    subject = await _subject_id(engine, session_id)
    client.post(f"/api/v1/subjects/{subject}/erase", headers=scope)

    hits = client.get(
        "/api/v1/memories/search", headers=scope, params={"q": "flamingo"}
    )

    assert hits.status_code == 200
    assert hits.json() == []
