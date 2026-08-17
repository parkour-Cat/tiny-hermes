"""Widening the budget a safety valve stopped a Run on.

Product design §12.3: reaching a limit puts the Run in `paused(limit)`, writes
`run_limit_reached`, and forbids further scheduling **未由有权限主体明确扩大
预算前** — so the widening is a real operation someone performs and it is
audited, not a value that decays or a retry that quietly starts over.

The rule these tests exist for is the last clause of §12.3: 新重试只继承根预算
的剩余额度，不得因创建新 Run 重置安全阀. Expanding is raising a ceiling. It
never touches a `consumed_*` counter, so a Run that has already spent 20 model
calls and is given 40 has 20 left, not 40.
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


def _worker(engine: AsyncEngine, workspace_id: str) -> WorkerRuntime:
    return WorkerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        model=DeterministicModelProvider(delay_ms=0),
        notifier=NullWakeUpNotifier(),
        settings=WorkerSettings(
            worker_id="worker-a",
            lease_seconds=30,
            max_slice_seconds=30,
            idle_poll_seconds=1,
            workspace_id=UUID(workspace_id),
        ),
    )


async def _events(engine: AsyncEngine, run_id: Any) -> list[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT event_type FROM run_events WHERE run_id = :id ORDER BY sequence"),
            {"id": UUID(str(run_id))},
        )
        return [str(row.event_type) for row in rows.all()]


async def maxima(engine: AsyncEngine, root_run_id: Any) -> dict[str, int]:
    """The one ceiling this endpoint moves, beside every counter it must not."""
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT max_model_calls, consumed_model_calls, version, "
                    "consumed_execution_ms, consumed_tokens "
                    "FROM run_budget_scopes WHERE root_run_id = :id"
                ),
                {"id": UUID(str(root_run_id))},
            )
        ).one()
    return {
        "max": int(row[0]),
        "consumed": int(row[1]),
        "version": int(row[2]),
        "execution_ms": int(row[3]),
        "tokens": int(row[4]),
    }


async def audits(engine: AsyncEngine, workspace_id: str) -> list[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT action FROM audit_events WHERE workspace_id = :id "
                "ORDER BY created_at"
            ),
            {"id": UUID(workspace_id)},
        )
        return [str(row.action) for row in rows.all()]


async def stopped_at_the_limit(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> dict[str, Any]:
    """A Run that has used every model call its budget allows."""
    session_id = session_for(agent_with_scenario("continue_once"))
    run = submit_run(session_id, "key-1")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE run_budget_scopes "
                "SET consumed_model_calls = max_model_calls - 1 WHERE root_run_id = :id"
            ),
            {"id": UUID(str(run["budget_root_run_id"]))},
        )
    await _worker(engine, scope["X-Workspace-Id"]).run_once()
    reloaded = client.get(f"/api/v1/runs/{run['id']}", headers=scope).json()
    assert reloaded["status"] == "paused"
    assert reloaded["pause_reason"] == "limit"
    return dict(reloaded)


async def test_a_paused_run_offers_no_resume_until_the_budget_is_widened(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    """The valve is the state of the budget, not a flag someone can clear."""
    paused = await stopped_at_the_limit(
        client, scope, engine, agent_with_scenario, session_for, submit_run
    )

    assert paused["available_actions"] == ["cancel"]


async def test_widening_the_budget_reopens_the_run_without_resetting_a_counter(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    paused = await stopped_at_the_limit(
        client, scope, engine, agent_with_scenario, session_for, submit_run
    )
    before = await maxima(engine, paused["budget_root_run_id"])

    widened = client.post(
        f"/api/v1/runs/{paused['id']}/budget",
        headers=scope,
        json={
            "expected_state_version": paused["state_version"],
            "max_model_calls": before["max"] + 5,
        },
    )

    assert widened.status_code == 200
    after = await maxima(engine, paused["budget_root_run_id"])
    assert after["max"] == before["max"] + 5
    assert after["consumed"] == before["consumed"]
    assert widened.json()["budget"]["max_model_calls"] == before["max"] + 5
    assert "resume" in widened.json()["available_actions"]


async def test_the_run_goes_back_to_work_and_spends_the_room_it_was_given(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    """Widening alone does not restart anything — someone still resumes it."""
    paused = await stopped_at_the_limit(
        client, scope, engine, agent_with_scenario, session_for, submit_run
    )
    spent_before = await maxima(engine, paused["budget_root_run_id"])
    widened = client.post(
        f"/api/v1/runs/{paused['id']}/budget",
        headers=scope,
        json={
            "expected_state_version": paused["state_version"],
            "max_model_calls": 40,
        },
    ).json()

    resumed = client.post(
        f"/api/v1/runs/{paused['id']}/resume",
        headers=scope,
        json={"expected_state_version": widened["state_version"]},
    )
    assert resumed.status_code == 200
    await _worker(engine, scope["X-Workspace-Id"]).run_once()

    reloaded = client.get(f"/api/v1/runs/{paused['id']}", headers=scope).json()
    assert reloaded["status"] == "completed"
    spent_after = await maxima(engine, paused["budget_root_run_id"])
    # Every counter moved forward from where it already was. None of them
    # started again from zero, which is the whole point of §12.3's last rule.
    assert spent_after["consumed"] > spent_before["consumed"]
    assert spent_after["execution_ms"] >= spent_before["execution_ms"]
    assert spent_after["tokens"] >= spent_before["tokens"]
    assert "run_limit_reached" in await _events(engine, paused["id"])


async def test_a_value_that_is_not_larger_is_refused(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    """This endpoint only widens.

    Lowering a ceiling under a Run that is spending against it would be a
    different operation with a different question to answer — what happens to
    work already in flight — and §12.3 does not ask for one.
    """
    paused = await stopped_at_the_limit(
        client, scope, engine, agent_with_scenario, session_for, submit_run
    )
    current = (await maxima(engine, paused["budget_root_run_id"]))["max"]

    refused = client.post(
        f"/api/v1/runs/{paused['id']}/budget",
        headers=scope,
        json={
            "expected_state_version": paused["state_version"],
            "max_model_calls": current,
        },
    )

    assert refused.status_code == 422
    assert refused.json()["code"] == "budget_not_widened"
    assert (await maxima(engine, paused["budget_root_run_id"]))["max"] == current


async def test_a_stale_state_version_is_refused_like_any_other_control(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    paused = await stopped_at_the_limit(
        client, scope, engine, agent_with_scenario, session_for, submit_run
    )

    stale = client.post(
        f"/api/v1/runs/{paused['id']}/budget",
        headers=scope,
        json={
            "expected_state_version": paused["state_version"] + 1,
            "max_model_calls": 40,
        },
    )

    assert stale.status_code == 409


async def test_the_widening_is_written_to_the_audit_log(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> None:
    """§12.3: 扩大预算必须由有权限的主体明确操作并写审计."""
    paused = await stopped_at_the_limit(
        client, scope, engine, agent_with_scenario, session_for, submit_run
    )

    client.post(
        f"/api/v1/runs/{paused['id']}/budget",
        headers=scope,
        json={
            "expected_state_version": paused["state_version"],
            "max_model_calls": 40,
        },
    )

    assert "run.budget_widened" in await audits(engine, scope["X-Workspace-Id"])
