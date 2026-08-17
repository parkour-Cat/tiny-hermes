"""Progressive loading, from the catalog to the model and back.

Design §10.1: what a bound skill costs every round is one line, and the
document costs nothing until the model asks for it by name. That is a claim
about three components that only meet in a running Run — the skill catalog, the
Agent Version that bound a version of it, and the Worker that answers
`skill.load` — so it is asserted here rather than anywhere cheaper.

§10.2's two authorization steps are both visible in this file. The first is
what the model is *told*: summaries of the skills this Version bound, and
nothing about any other skill in the workspace. The second is what actually
runs: a load is authorized against the Run's own bound versions, so a model
that names a skill it was not given is refused exactly as if it had named a
tool it was not given.

None of it needs a container. `skill.load` is a platform tool, so this whole
suite runs on a host with no sandbox image.
"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.infrastructure.skill_library import SqlSkillLibrary
from tiny_hermes.runs.ports.model import ModelRequest, ModelResponse

from ..conftest import VALID_SPEC

BODY = "Take the machine out of the pool first, then drain it. " * 8

SKILL_MD = f"""---
name: rollout
description: How this company takes a machine out of rotation before a deploy.
---

# Rollout

{BODY}
"""

OTHER_MD = """---
name: postmortem
description: What a postmortem must answer before it is circulated.
---

# Postmortem

Say what the customer saw.
"""


class Recorder:
    """The deterministic provider, with every request it answered kept.

    "The summary is in the request and the document is not" is a statement
    about the bytes sent to the model, and this is the only place that can see
    them.
    """

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
        skills=SqlSkillLibrary(sessions),
        settings=WorkerSettings(
            worker_id="worker-a",
            lease_seconds=30,
            max_slice_seconds=30,
            idle_poll_seconds=1,
            workspace_id=UUID(workspace_id),
        ),
    )


def _skill(client: TestClient, scope: dict[str, str], document: str) -> str:
    """A skill in the catalog. Returns the id of its first version."""
    created = client.post(
        "/api/v1/skills",
        headers=scope,
        json={"scope": "workspace", "files": [{"path": "SKILL.md", "content": document}]},
    )
    assert created.status_code == 201, created.text
    versions = client.get(f"/api/v1/skills/{created.json()['id']}/versions", headers=scope)
    return str(versions.json()[0]["id"])


def _agent(
    client: TestClient, scope: dict[str, str], version_ids: list[str], alias: str = "loader"
) -> str:
    agent_id = str(
        client.post(
            "/api/v1/agents", headers=scope, json={"name": alias.title(), "alias": alias}
        ).json()["id"]
    )
    spec = {
        **VALID_SPEC,
        "model_policy": {"provider": "deterministic", "scenario": "skill_once"},
        "tools": ["skill.load"],
        "skills": [{"skill_version_id": value} for value in version_ids],
    }
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


def _ask(client: TestClient, scope: dict[str, str], session_id: str, name: str) -> dict[str, Any]:
    """Start a Run whose input is the skill the drill should load."""
    created = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": f"skill-{name}"},
        json={"session_id": session_id, "input": name},
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


async def _events(engine: AsyncEngine, run_id: Any) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT event_type, payload FROM run_events "
                "WHERE run_id = :id ORDER BY sequence"
            ),
            {"id": UUID(str(run_id))},
        )
        return [{"type": str(row[0]), "payload": row[1]} for row in rows.all()]


def _transcript(client: TestClient, scope: dict[str, str], session_id: str) -> str:
    page = client.get(f"/api/v1/sessions/{session_id}/messages", headers=scope)
    assert page.status_code == 200, page.text
    return page.text


async def test_the_summary_is_in_the_first_request_and_the_document_is_not(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """What a bound skill costs before it is loaded: one line about it."""
    version_id = _skill(client, scope, SKILL_MD)
    session_id = session_for(_agent(client, scope, [version_id]))
    _ask(client, scope, session_id, "rollout")
    model = Recorder()

    await _worker(engine, scope["X-Workspace-Id"], model).run_once()

    first = model.requests[0]
    assert len(first.skill_summaries) == 1
    assert "rollout" in first.skill_summaries[0]
    assert "before a deploy" in first.skill_summaries[0]
    assert BODY not in "".join(first.skill_summaries)


async def test_the_model_asks_and_the_document_comes_back_as_a_tool_result(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    version_id = _skill(client, scope, SKILL_MD)
    session_id = session_for(_agent(client, scope, [version_id]))
    run = _ask(client, scope, session_id, "rollout")

    await _worker(engine, scope["X-Workspace-Id"], Recorder()).run_once()

    reloaded = client.get(f"/api/v1/runs/{run['id']}", headers=scope).json()
    assert reloaded["status"] == "completed", reloaded
    assert BODY.strip() in _transcript(client, scope, session_id)


async def test_a_load_leaves_an_event_saying_which_version_was_read(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """Not derived from a signal, like `goal_verdict` and `context_trimmed`.

    The event is what makes 命中 a fact rather than a guess: the next round's
    planner reads it to decide which summaries it may not remove.
    """
    version_id = _skill(client, scope, SKILL_MD)
    session_id = session_for(_agent(client, scope, [version_id]))
    run = _ask(client, scope, session_id, "rollout")

    await _worker(engine, scope["X-Workspace-Id"], Recorder()).run_once()

    loaded = [item for item in await _events(engine, run["id"]) if item["type"] == "skill_loaded"]
    assert len(loaded) == 1
    payload = loaded[0]["payload"]
    assert payload["skill"] == "rollout"
    assert payload["path"] == "SKILL.md"
    assert payload["skill_version_id"] == version_id
    assert payload["bytes"] == len(SKILL_MD.encode())


async def test_the_second_round_knows_the_skill_was_already_loaded(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """The whole reason the event exists, seen from the round that reads it."""
    version_id = _skill(client, scope, SKILL_MD)
    session_id = session_for(_agent(client, scope, [version_id]))
    _ask(client, scope, session_id, "rollout")
    model = Recorder()

    await _worker(engine, scope["X-Workspace-Id"], model).run_once()

    assert len(model.requests) == 2
    # Both rounds are told about the skill; the second is told about it while
    # the document is already in the conversation, and keeps the line anyway.
    assert model.requests[1].skill_summaries == model.requests[0].skill_summaries


async def test_a_skill_this_version_did_not_bind_is_not_authorized(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """The second authorization check, against the Run rather than the model.

    `postmortem` exists, is in this workspace, and is perfectly readable
    through the catalog API. This Agent did not bind it, so as far as this Run
    is concerned it does not exist — the same answer a model gets when it names
    a tool the Version did not bind.
    """
    bound = _skill(client, scope, SKILL_MD)
    _skill(client, scope, OTHER_MD)
    session_id = session_for(_agent(client, scope, [bound]))
    run = _ask(client, scope, session_id, "postmortem")

    await _worker(engine, scope["X-Workspace-Id"], Recorder()).run_once()

    reloaded = client.get(f"/api/v1/runs/{run['id']}", headers=scope).json()
    assert reloaded["status"] == "failed"
    transcript = _transcript(client, scope, session_id)
    assert "tool_not_authorized" in transcript
    assert "Say what the customer saw." not in transcript


async def test_the_model_is_never_told_about_a_skill_it_cannot_load(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """The first authorization check. The refusal above is the backstop."""
    bound = _skill(client, scope, SKILL_MD)
    _skill(client, scope, OTHER_MD)
    session_id = session_for(_agent(client, scope, [bound]))
    _ask(client, scope, session_id, "rollout")
    model = Recorder()

    await _worker(engine, scope["X-Workspace-Id"], model).run_once()

    summaries = "".join(model.requests[0].skill_summaries)
    assert "rollout" in summaries
    assert "postmortem" not in summaries


async def test_the_library_reads_one_file_out_of_a_version_and_no_other(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine
) -> None:
    """The port the Worker loads through, on its own.

    A path the package does not have answers `None` rather than raising: a
    model naming a file that is not there made an ordinary mistake, and the
    answer it needs is a tool result saying so. A path belonging to a
    *different* version answers `None` too, which is the version boundary the
    whole design rests on — the Run holds version ids, not skill names.
    """
    version_id = _skill(client, scope, SKILL_MD)
    other_id = _skill(client, scope, OTHER_MD)
    library = SqlSkillLibrary(async_sessionmaker(engine, expire_on_commit=False))

    assert await library.read_file(UUID(version_id), "SKILL.md") == SKILL_MD
    assert await library.read_file(UUID(version_id), "reference/nothing.md") is None
    assert await library.read_file(UUID(other_id), "SKILL.md") == OTHER_MD
