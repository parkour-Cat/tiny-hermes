"""An Agent suggesting a change to its own skill, and getting no further.

§15.3 from the Run's side. The Agent reads a skill it was bound to, proposes a
better version of it, and the Run ends — with a `pending` row, no new version,
and its own binding untouched. Everything after that step needs a person.

This is the roadmap's "the model's judgment is only a suggestion" as an
executable claim rather than a sentence in a document: there is no path from
`skill.propose` to a version that does not pass through an approval nobody in
this file performs.
"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.deterministic_model import (
    PROPOSED_LINE,
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.infrastructure.skill_library import SqlSkillLibrary
from tiny_hermes.runs.infrastructure.skill_proposals import SqlSkillProposals

from ..conftest import VALID_SPEC

SKILL_MD = """---
name: rollout
description: How this company takes a machine out of rotation before a deploy.
---

# Rollout

Take the machine out of the pool first, then drain it.
"""


def _worker(engine: AsyncEngine, workspace_id: str) -> WorkerRuntime:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return WorkerRuntime(
        session_factory=sessions,
        model=DeterministicModelProvider(delay_ms=0),
        notifier=NullWakeUpNotifier(),
        skills=SqlSkillLibrary(sessions),
        proposals=SqlSkillProposals(sessions),
        settings=WorkerSettings(
            worker_id="worker-a",
            lease_seconds=30,
            max_slice_seconds=30,
            idle_poll_seconds=1,
            workspace_id=UUID(workspace_id),
        ),
    )


def _skill(client: TestClient, scope: dict[str, str]) -> dict[str, Any]:
    created = client.post(
        "/api/v1/skills",
        headers=scope,
        json={"scope": "workspace", "files": [{"path": "SKILL.md", "content": SKILL_MD}]},
    )
    assert created.status_code == 201, created.text
    skill = created.json()
    versions = client.get(f"/api/v1/skills/{skill['id']}/versions", headers=scope).json()
    return {"skill": skill, "version": versions[0]}


def _agent(client: TestClient, scope: dict[str, str], version_id: str) -> str:
    agent_id = str(
        client.post(
            "/api/v1/agents", headers=scope, json={"name": "Author", "alias": "author"}
        ).json()["id"]
    )
    draft = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={
            "expected_revision": 1,
            "spec": {
                **VALID_SPEC,
                "model_policy": {"provider": "deterministic", "scenario": "propose_once"},
                "tools": ["skill.propose"],
                "skills": [{"skill_version_id": version_id}],
            },
        },
    )
    assert draft.status_code == 200, draft.text
    published = client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=scope,
        json={"expected_revision": draft.json()["revision"]},
    )
    assert published.status_code == 201, published.text
    return agent_id


def _run(client: TestClient, scope: dict[str, str], session_id: str, key: str) -> dict[str, Any]:
    created = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": key},
        json={"session_id": session_id, "input": "rollout"},
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


async def test_an_agent_opens_a_proposal_and_the_run_ends_there(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    existing = _skill(client, scope)
    session_id = session_for(_agent(client, scope, existing["version"]["id"]))
    run = _run(client, scope, session_id, "propose-1")

    await _worker(engine, scope["X-Workspace-Id"]).run_once()

    reloaded = client.get(f"/api/v1/runs/{run['id']}", headers=scope).json()
    assert reloaded["status"] == "completed", reloaded
    queue = client.get("/api/v1/skill-proposals", headers=scope).json()
    assert len(queue) == 1
    assert queue[0]["origin"] == "agent"
    assert queue[0]["origin_run_id"] == run["id"]
    assert queue[0]["status"] == "pending"
    # No version was created, and the skill still offers the one it had.
    versions = client.get(
        f"/api/v1/skills/{existing['skill']['id']}/versions", headers=scope
    ).json()
    assert [item["id"] for item in versions] == [existing["version"]["id"]]


async def test_the_proposal_leaves_a_mark_on_the_run_that_made_it(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """A suggestion nobody can trace back to a Run is a suggestion a reviewer
    has to take on faith."""
    existing = _skill(client, scope)
    session_id = session_for(_agent(client, scope, existing["version"]["id"]))
    run = _run(client, scope, session_id, "propose-1")

    await _worker(engine, scope["X-Workspace-Id"]).run_once()

    proposed = [
        item for item in await _events(engine, run["id"]) if item["type"] == "skill_proposed"
    ]
    assert len(proposed) == 1
    queue = client.get("/api/v1/skill-proposals", headers=scope).json()
    assert proposed[0]["payload"]["proposal_id"] == queue[0]["id"]
    assert proposed[0]["payload"]["skill"] == "rollout"


async def test_the_diff_a_reviewer_sees_is_against_the_version_the_run_held(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """Not against whatever is newest. The Agent wrote its suggestion having
    read one particular document, and that is the one it should be read
    against."""
    existing = _skill(client, scope)
    session_id = session_for(_agent(client, scope, existing["version"]["id"]))
    _run(client, scope, session_id, "propose-1")
    await _worker(engine, scope["X-Workspace-Id"]).run_once()
    opened = client.get("/api/v1/skill-proposals", headers=scope).json()[0]

    read = client.get(f"/api/v1/skill-proposals/{opened['id']}", headers=scope).json()

    assert read["base_version_id"] == existing["version"]["id"]
    changed = read["diff"][0]
    assert changed["path"] == "SKILL.md"
    assert any(PROPOSED_LINE in line["text"] for line in changed["lines"])


async def test_one_run_proposes_once_however_many_times_it_is_worked(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """Two Runs in one Session are two allowances; one Run is one."""
    existing = _skill(client, scope)
    session_id = session_for(_agent(client, scope, existing["version"]["id"]))
    _run(client, scope, session_id, "propose-1")
    await _worker(engine, scope["X-Workspace-Id"]).run_once()
    _run(client, scope, session_id, "propose-2")
    await _worker(engine, scope["X-Workspace-Id"]).run_once()

    queue = client.get("/api/v1/skill-proposals", headers=scope).json()

    assert len(queue) == 2
    assert len({item["origin_run_id"] for item in queue}) == 2


async def test_approving_the_agents_proposal_does_not_change_what_it_runs(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """§15.3's last sentence, end to end.

    The Agent proposed, a person approved, a version exists — and the Agent is
    still running the version it was published with. Switching is a republish.
    """
    existing = _skill(client, scope)
    agent_id = _agent(client, scope, existing["version"]["id"])
    session_id = session_for(agent_id)
    _run(client, scope, session_id, "propose-1")
    await _worker(engine, scope["X-Workspace-Id"]).run_once()
    opened = client.get("/api/v1/skill-proposals", headers=scope).json()[0]

    approved = client.post(
        f"/api/v1/skill-proposals/{opened['id']}/approve", headers=scope
    )

    assert approved.status_code == 201, approved.text
    assert approved.json()["version_number"] == 2
    agent = client.get(f"/api/v1/agents/{agent_id}", headers=scope).json()
    running = client.get(
        f"/api/v1/agents/{agent_id}/versions/{agent['current_version_id']}", headers=scope
    ).json()
    assert running["spec"]["skills"] == [{"skill_version_id": existing["version"]["id"]}]
