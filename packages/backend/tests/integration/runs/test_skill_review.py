"""After a Run with enough tool calls completes, one more call asks whether the
work is worth a skill (§15.4, v2.12).

What it may produce is a `pending` proposal on the same path `skill.propose`
takes — never a version. And it happens after the outcome is recorded, so the
Run is `completed` whether the review proposes, declines or fails.
"""

import json
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.domain.models import TextBlock, ToolCallBlock
from tiny_hermes.runs.domain.skill_review import REVIEW_MARKER
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.infrastructure.skill_library import SqlSkillLibrary
from tiny_hermes.runs.infrastructure.skill_proposals import SqlSkillProposals
from tiny_hermes.runs.ports.model import ModelRequest, ModelResponse, StopReason

from ..conftest import VALID_SPEC
from .test_worker_tools import StandInSandbox

NEW_SKILL = """---
name: drain-host
description: Take a host out of the pool and drain it before maintenance.
---

# Drain a host

1. Remove it from the pool.
2. Wait for connections to reach zero.
"""

ROLLOUT = """---
name: rollout
description: How this company takes a machine out of rotation before a deploy.
---

# Rollout

Take the machine out of the pool first, then drain it.
"""


def _is_review(request: ModelRequest) -> bool:
    first = request.messages[0].blocks[0] if request.messages else None
    return isinstance(first, TextBlock) and REVIEW_MARKER in first.text


class Reviewed:
    """Two shell rounds, then done; the review call gets ``review``."""

    def __init__(self, review: str, tool_rounds: int = 2) -> None:
        self._review = review
        self._left = tool_rounds
        self.reviews: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if _is_review(request):
            self.reviews.append(request)
            return ModelResponse(
                stop_reason=StopReason.COMPLETED,
                text=self._review,
                input_tokens=40,
                output_tokens=20,
            )
        if self._left > 0:
            self._left -= 1
            return ModelResponse(
                stop_reason=StopReason.TOOL_CALL,
                text="",
                tool_calls=(
                    ToolCallBlock(
                        call_id=f"c{self._left}", name="shell.exec", arguments={"command": "ls"}
                    ),
                ),
                input_tokens=10,
                output_tokens=5,
            )
        return ModelResponse(
            stop_reason=StopReason.COMPLETED, text="drained", input_tokens=10, output_tokens=5
        )


def _skill(client: TestClient, scope: dict[str, str]) -> str:
    created = client.post(
        "/api/v1/skills",
        headers=scope,
        json={"scope": "workspace", "files": [{"path": "SKILL.md", "content": ROLLOUT}]},
    )
    assert created.status_code == 201, created.text
    versions = client.get(f"/api/v1/skills/{created.json()['id']}/versions", headers=scope)
    return str(versions.json()[0]["id"])


def _agent(
    client: TestClient,
    scope: dict[str, str],
    review: dict[str, Any] | None,
    skills: list[str] | None = None,
    limits: dict[str, Any] | None = None,
) -> str:
    agent_id = str(
        client.post(
            "/api/v1/agents", headers=scope, json={"name": "Ops", "alias": "ops"}
        ).json()["id"]
    )
    spec: dict[str, Any] = {**VALID_SPEC, "tools": ["shell.exec"]}
    if review is not None:
        spec["skill_review"] = review
    if skills:
        spec["skills"] = [{"skill_version_id": item} for item in skills]
    if limits:
        spec["limits"] = {**VALID_SPEC["limits"], **limits}
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


def _submit(client: TestClient, scope: dict[str, str], agent_id: str) -> str:
    session = client.post("/api/v1/sessions", headers=scope, json={"agent_id": agent_id}).json()
    created = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": f"review-{session['id']}"},
        json={"session_id": session["id"], "input": "drain web-3"},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def _drive(engine: AsyncEngine, workspace_id: str, model: Any) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    sandbox: Any = StandInSandbox()
    await WorkerRuntime(
        session_factory=sessions,
        model=model,
        notifier=NullWakeUpNotifier(),
        sandbox=sandbox,
        skills=SqlSkillLibrary(sessions),
        proposals=SqlSkillProposals(sessions),
        settings=WorkerSettings(
            worker_id="worker-review",
            lease_seconds=30,
            max_slice_seconds=30,
            idle_poll_seconds=1,
            workspace_id=UUID(workspace_id),
        ),
    ).run_once()


async def _reviews(engine: AsyncEngine, run: str) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT payload FROM run_events WHERE run_id = :id "
                "AND event_type = 'skill_review' ORDER BY sequence"
            ),
            {"id": run},
        )
        return [dict(row[0]) for row in rows.all()]


def _status(client: TestClient, scope: dict[str, str], run: str) -> dict[str, Any]:
    return dict(client.get(f"/api/v1/runs/{run}", headers=scope).json())


async def test_enough_tool_calls_open_a_pending_proposal_and_the_run_stays_completed(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine
) -> None:
    run = _submit(client, scope, _agent(client, scope, {"min_tool_calls": 2}))
    model = Reviewed(json.dumps({"decision": "new", "skill_md": NEW_SKILL}))

    await _drive(engine, scope["X-Workspace-Id"], model)

    snapshot = _status(client, scope, run)
    assert snapshot["status"] == "completed"
    [proposal] = client.get("/api/v1/skill-proposals", headers=scope).json()
    assert (proposal["name"], proposal["status"], proposal["origin_run_id"]) == (
        "drain-host",
        "pending",
        run,
    )
    [review] = await _reviews(engine, run)
    assert review["outcome"] == "proposed"
    assert review["proposal_id"] == proposal["id"]
    # The review is a real call on the Run's budget: three rounds and itself.
    assert snapshot["budget"]["consumed_model_calls"] == 4
    # It read what the Run did.
    [asked] = model.reviews
    assert "drain web-3" in asked.messages[0].blocks[0].text  # type: ignore[union-attr]


async def test_fewer_tool_calls_than_asked_for_means_no_review(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine
) -> None:
    run = _submit(client, scope, _agent(client, scope, {"min_tool_calls": 3}))
    model = Reviewed(json.dumps({"decision": "none"}))

    await _drive(engine, scope["X-Workspace-Id"], model)

    assert model.reviews == []
    assert await _reviews(engine, run) == []


async def test_an_agent_that_did_not_opt_in_is_never_reviewed(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine
) -> None:
    run = _submit(client, scope, _agent(client, scope, None))
    model = Reviewed(json.dumps({"decision": "none"}), tool_rounds=12)

    await _drive(engine, scope["X-Workspace-Id"], model)

    assert model.reviews == []
    assert await _reviews(engine, run) == []


async def test_nothing_worth_keeping_opens_nothing(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine
) -> None:
    run = _submit(client, scope, _agent(client, scope, {"min_tool_calls": 2}))

    await _drive(engine, scope["X-Workspace-Id"], Reviewed(json.dumps({"decision": "none"})))

    assert client.get("/api/v1/skill-proposals", headers=scope).json() == []
    assert [item["outcome"] for item in await _reviews(engine, run)] == ["none"]


async def test_an_unreadable_reply_opens_nothing_and_says_so(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine
) -> None:
    run = _submit(client, scope, _agent(client, scope, {"min_tool_calls": 2}))

    await _drive(engine, scope["X-Workspace-Id"], Reviewed("Sure! Here is a skill: ..."))

    assert _status(client, scope, run)["status"] == "completed"
    assert client.get("/api/v1/skill-proposals", headers=scope).json() == []
    assert [item["outcome"] for item in await _reviews(engine, run)] == ["unreadable"]


async def test_a_patch_is_against_the_version_this_run_was_given(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine
) -> None:
    version = _skill(client, scope)
    run = _submit(client, scope, _agent(client, scope, {"min_tool_calls": 2}, skills=[version]))
    better = ROLLOUT + "\nCheck the pool is healthy before draining.\n"

    await _drive(
        engine,
        scope["X-Workspace-Id"],
        Reviewed(json.dumps({"decision": "patch", "skill": "rollout", "skill_md": better})),
    )

    [proposal] = client.get("/api/v1/skill-proposals", headers=scope).json()
    assert proposal["base_version_id"] == version
    assert [item["outcome"] for item in await _reviews(engine, run)] == ["proposed"]


async def test_a_patch_to_a_skill_this_agent_was_not_given_is_refused(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine
) -> None:
    run = _submit(client, scope, _agent(client, scope, {"min_tool_calls": 2}))

    await _drive(
        engine,
        scope["X-Workspace-Id"],
        Reviewed(json.dumps({"decision": "patch", "skill": "rollout", "skill_md": ROLLOUT})),
    )

    assert client.get("/api/v1/skill-proposals", headers=scope).json() == []
    assert [item["outcome"] for item in await _reviews(engine, run)] == ["refused"]
