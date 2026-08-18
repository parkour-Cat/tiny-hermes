"""Retrieving past conversations, and the one thing a search must never do.

§14.3 gives a Run a way to look back through what was said before, on demand
and in snippets. The roadmap's fifth exit criterion is the constraint: a search
returns **only sessions the requester may read**.

That is asserted here rather than in the store because it is the seam it would
fail at — the tool takes no scope argument, so the whole guarantee is that the
adapter reads the subject off the Run, and a wrong join there looks like nothing
at all until somebody sees another person's words in their own transcript.
"""

from collections.abc import Callable
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.memory.infrastructure.run_searches import SqlRunSessionSearches
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier


def _worker(engine: AsyncEngine, workspace_id: str) -> WorkerRuntime:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return WorkerRuntime(
        session_factory=sessions,
        model=DeterministicModelProvider(delay_ms=0),
        notifier=NullWakeUpNotifier(),
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
        headers={**scope, "Idempotency-Key": f"search-{body[:14]}-{session_id[:8]}"},
        json={"session_id": session_id, "input": body},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def _transcript(engine: AsyncEngine, run_id: str) -> str:
    """Everything this Run said, as one string, so a test can look for a
    snippet in what the model actually answered with."""
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT content::text FROM session_messages "
                "WHERE source_run_id = :r ORDER BY sequence"
            ),
            {"r": UUID(run_id)},
        )
        return " ".join(str(r[0]) for r in rows.all())


async def _reassign_session(
    engine: AsyncEngine, session_id: str, caller_id: UUID
) -> None:
    """Move a Session to another subject.

    The console has one signed-in person, so a second subject is made by
    changing who a Session belongs to — which is exactly the column the search
    joins on, and therefore the right thing to vary.
    """
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE sessions SET caller_id = :c WHERE id = :s"),
            {"c": caller_id, "s": UUID(session_id)},
        )


@pytest.fixture
def searcher(agent_with_scenario: Callable[..., str]) -> str:
    return agent_with_scenario(
        "search_once", alias="searcher", tools=["session.search"]
    )


async def test_a_run_finds_what_this_subject_said_before(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    searcher: str,
) -> None:
    workspace_id = scope["X-Workspace-Id"]
    earlier = session_for(searcher)
    _run(client, scope, earlier, "the pelican rollout is on Tuesday")
    await _worker(engine, workspace_id).run_once()

    later = session_for(searcher)
    found = _run(client, scope, later, "pelican")
    await _worker(engine, workspace_id).run_once()

    assert "pelican rollout" in await _transcript(engine, found)


async def test_a_search_does_not_reach_another_subject_s_sessions(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    searcher: str,
) -> None:
    """The fifth exit criterion. Same workspace, same Agent, different subject
    — and the words one of them used are not findable by the other."""
    workspace_id = scope["X-Workspace-Id"]
    theirs = session_for(searcher)
    _run(client, scope, theirs, "the albatross migration is on Friday")
    await _worker(engine, workspace_id).run_once()
    # That conversation now belongs to somebody else.
    await _reassign_session(engine, theirs, UUID(int=7))

    mine = session_for(searcher)
    found = _run(client, scope, mine, "albatross")
    await _worker(engine, workspace_id).run_once()

    transcript = await _transcript(engine, found)
    assert "albatross migration is on Friday" not in transcript
    assert "No past message matched" in transcript


async def test_a_search_with_no_match_says_so_rather_than_failing(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    searcher: str,
) -> None:
    workspace_id = scope["X-Workspace-Id"]
    found = _run(client, scope, session_for(searcher), "quetzal")
    await _worker(engine, workspace_id).run_once()

    assert "No past message matched" in await _transcript(engine, found)


async def test_the_console_search_is_scoped_to_the_workspace(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    searcher: str,
) -> None:
    workspace_id = scope["X-Workspace-Id"]
    _run(client, scope, session_for(searcher), "the kingfisher deploy is delayed")
    await _worker(engine, workspace_id).run_once()

    hits = client.get(
        "/api/v1/memories/search", headers=scope, params={"q": "kingfisher"}
    )

    assert hits.status_code == 200, hits.text
    assert any("kingfisher" in item["snippet"] for item in hits.json())


async def test_an_empty_console_query_is_refused(
    client: TestClient, scope: dict[str, str], searcher: str
) -> None:
    refused = client.get(
        "/api/v1/memories/search", headers=scope, params={"q": "   "}
    )

    assert refused.status_code == 422
