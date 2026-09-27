"""What a reviewer can see about how a skill version has done (§15.4, v2.12).

Counted per Run, not per load, and kept in a table of its own: a terminal
Run's events are pruned after the retention window, and a count read off them
would drift to zero without anyone deciding it should.
"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.infrastructure.skill_library import SqlSkillLibrary
from tiny_hermes.runs.infrastructure.skill_proposals import SqlSkillProposals
from tiny_hermes.runs.ports.model import ModelRequest, ModelResponse, StopReason

from ..conftest import VALID_SPEC

SKILL_MD = """---
name: rollout
description: How this company takes a machine out of rotation before a deploy.
---

# Rollout

Take the machine out of the pool first, then drain it.
"""


class Scripted:
    def __init__(self, *answers: ModelResponse) -> None:
        self._answers = list(answers)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        if self._answers:
            return self._answers.pop(0)
        return ModelResponse(
            stop_reason=StopReason.COMPLETED, text="done", input_tokens=5, output_tokens=2
        )


def _calls(*calls: ToolCallBlock) -> ModelResponse:
    return ModelResponse(
        stop_reason=StopReason.TOOL_CALL, text="", tool_calls=calls, input_tokens=5, output_tokens=2
    )


def _load(call_id: str) -> ToolCallBlock:
    return ToolCallBlock(call_id=call_id, name="skill.load", arguments={"skill": "rollout"})


async def _drive(engine: AsyncEngine, workspace_id: str, model: Any) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    await WorkerRuntime(
        session_factory=sessions,
        model=model,
        notifier=NullWakeUpNotifier(),
        skills=SqlSkillLibrary(sessions),
        proposals=SqlSkillProposals(sessions),
        settings=WorkerSettings(
            worker_id="worker-usage",
            lease_seconds=30,
            max_slice_seconds=30,
            idle_poll_seconds=1,
            workspace_id=UUID(workspace_id),
        ),
    ).run_once()


def _skill(client: TestClient, scope: dict[str, str]) -> tuple[str, str]:
    created = client.post(
        "/api/v1/skills",
        headers=scope,
        json={"scope": "workspace", "files": [{"path": "SKILL.md", "content": SKILL_MD}]},
    )
    assert created.status_code == 201, created.text
    skill_id = str(created.json()["id"])
    versions = client.get(f"/api/v1/skills/{skill_id}/versions", headers=scope).json()
    return skill_id, str(versions[0]["id"])


def _agent(client: TestClient, scope: dict[str, str], version_id: str) -> str:
    agent_id = str(
        client.post(
            "/api/v1/agents", headers=scope, json={"name": "Deployer", "alias": "deployer"}
        ).json()["id"]
    )
    draft = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={
            "expected_revision": 1,
            "spec": {
                **VALID_SPEC,
                "tools": ["skill.load", "skill.propose"],
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


def _submit(client: TestClient, scope: dict[str, str], session_id: str, key: str) -> str:
    created = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": key},
        json={"session_id": session_id, "input": "take web-3 out"},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def _usage(client: TestClient, scope: dict[str, str], skill_id: str, version_id: str) -> Any:
    versions = client.get(f"/api/v1/skills/{skill_id}/versions", headers=scope).json()
    return next(item for item in versions if item["id"] == version_id)["usage"]


async def test_a_version_counts_the_runs_that_loaded_it_and_how_they_ended(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    skill_id, version_id = _skill(client, scope)
    agent = _agent(client, scope, version_id)
    assert _usage(client, scope, skill_id, version_id) == {
        "runs": 0,
        "completed": 0,
        "failed": 0,
    }

    _submit(client, scope, session_for(agent), "usage-1")
    # Loaded twice in one Run: still one Run.
    await _drive(engine, scope["X-Workspace-Id"], Scripted(_calls(_load("a")), _calls(_load("b"))))

    assert _usage(client, scope, skill_id, version_id) == {
        "runs": 1,
        "completed": 1,
        "failed": 0,
    }


async def test_the_count_outlives_the_run_s_events(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    skill_id, version_id = _skill(client, scope)
    run = _submit(client, scope, session_for(_agent(client, scope, version_id)), "usage-2")
    await _drive(engine, scope["X-Workspace-Id"], Scripted(_calls(_load("a"))))

    # What the scheduler's retention pass does to a terminal Run.
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM run_events WHERE run_id = :id"), {"id": run})

    assert _usage(client, scope, skill_id, version_id)["runs"] == 1


async def test_a_patch_proposal_shows_how_its_base_version_has_done(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    skill_id, version_id = _skill(client, scope)
    agent = _agent(client, scope, version_id)
    _submit(client, scope, session_for(agent), "usage-3")
    await _drive(engine, scope["X-Workspace-Id"], Scripted(_calls(_load("a"))))
    _submit(client, scope, session_for(agent), "usage-4")
    patched = SKILL_MD + "\nCheck the pool is healthy before draining.\n"
    await _drive(
        engine,
        scope["X-Workspace-Id"],
        Scripted(
            _calls(
                ToolCallBlock(
                    call_id="p",
                    name="skill.propose",
                    arguments={
                        "skill": "rollout",
                        "files": [{"path": "SKILL.md", "content": patched}],
                    },
                )
            )
        ),
    )

    [proposal] = client.get("/api/v1/skill-proposals", headers=scope).json()
    detail = client.get(f"/api/v1/skill-proposals/{proposal['id']}", headers=scope).json()
    assert detail["base_usage"] == {"runs": 1, "completed": 1, "failed": 0}
    del skill_id
