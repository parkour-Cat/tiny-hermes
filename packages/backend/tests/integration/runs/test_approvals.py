"""A write that stops for a person, and what happens when they answer.

§16.3 end to end. The pieces only meet in a running Run: the Version that chose
`governance` at publish, the Worker that stops rather than writes, the row a
person decides, and the state machine that puts the Run back in the queue.

The stand-in API is the witness throughout. "The Run waited" is only worth
asserting alongside "nothing arrived at the far end", because a call whose
answer was discarded looks identical from the Run's side and is not the same
thing at all.

The helpers come from `http_tool_support` rather than being copied. This suite
and `test_http_tool_calls` describe two halves of one path, and a second copy of
"register a tool and publish an Agent" would be a second place for them to
drift.
"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.scheduler import (
    SchedulerRuntime,
    SchedulerSettings,
)
from tiny_hermes.runs.domain.approval import (
    ApprovalType,
    NormalizedCall,
    normalize_call,
)
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.infrastructure.sql_approvals import SqlApprovalGate
from tiny_hermes.runs.ports.approvals import ApprovalCheck, ApprovalVerdict

from ..conftest import VALID_SPEC
from ..egress_support import ProxyHandle
from .http_tool_support import StandIn, approve_host, ask, register_tool, worker


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


def _agent(
    client: TestClient,
    scope: dict[str, str],
    version_id: str,
    write_policy: str,
) -> str:
    agent_id = str(
        client.post(
            "/api/v1/agents", headers=scope, json={"name": "Writer", "alias": "writer"}
        ).json()["id"]
    )
    spec = {
        **VALID_SPEC,
        "model_policy": {"provider": "deterministic", "scenario": "http_once"},
        "network": {"allow": ["127.0.0.1"]},
        "http_tools": [
            {
                "http_tool_version_id": version_id,
                "operations": ["listOrders", "createOrder"],
                "write_policy": write_policy,
            }
        ],
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


def _pending(client: TestClient, scope: dict[str, str]) -> list[dict[str, Any]]:
    page = client.get("/api/v1/approvals", headers=scope)
    assert page.status_code == 200, page.text
    return list(page.json())


def _decide(
    client: TestClient,
    scope: dict[str, str],
    approval_id: str,
    decision: str,
    reason: str | None = None,
) -> Any:
    return client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        headers=scope,
        json={"decision": decision, "reason": reason},
    )


def _status(client: TestClient, scope: dict[str, str], run_id: Any) -> dict[str, Any]:
    return dict(client.get(f"/api/v1/runs/{run_id}", headers=scope).json())


async def _stopped_run(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> tuple[dict[str, Any], StandIn]:
    """A Run that asked to write, and stopped. The state every test starts in."""
    stand_in, url = api
    approve_host(client, scope, "127.0.0.1")
    version_id = register_tool(client, scope, url)
    session_id = session_for(_agent(client, scope, version_id, "governance"))
    run = ask(client, scope, session_id, "http.orders.createOrder")

    await worker(engine, scope["X-Workspace-Id"], proxy).run_once()

    return run, stand_in


async def test_a_governance_write_stops_the_run_and_nothing_reaches_the_api(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    run, stand_in = await _stopped_run(client, scope, engine, session_for, api, proxy)

    assert _status(client, scope, run["id"])["status"] == "waiting_approval"
    assert stand_in.requests == []
    waiting = _pending(client, scope)
    assert len(waiting) == 1
    assert waiting[0]["approval_type"] == "governance_approval"
    assert waiting[0]["tool"] == "http.orders.createOrder"


async def test_the_person_deciding_is_shown_the_request_that_would_be_sent(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """The document is the one that was hashed. A reviewer deciding from a
    summary the platform rewrote would be approving something nobody can prove
    matches what runs."""
    await _stopped_run(client, scope, engine, session_for, api, proxy)

    document = _pending(client, scope)[0]["document"]

    assert document["tool"] == "http.orders.createOrder"
    assert document["target"].endswith("/orders")
    assert document["required_permission"] == "http.orders.write"


async def test_approving_puts_the_run_back_in_the_queue_and_the_write_happens(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    run, stand_in = await _stopped_run(client, scope, engine, session_for, api, proxy)

    decided = _decide(client, scope, _pending(client, scope)[0]["id"], "approve")
    assert decided.status_code == 200, decided.text
    assert _status(client, scope, run["id"])["status"] == "queued"

    await worker(engine, scope["X-Workspace-Id"], proxy).run_once()

    assert stand_in.methods == ["POST"]
    assert _status(client, scope, run["id"])["status"] == "completed"


async def test_rejecting_pauses_the_run_with_the_reason_it_was_given(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    run, stand_in = await _stopped_run(client, scope, engine, session_for, api, proxy)

    decided = _decide(
        client, scope, _pending(client, scope)[0]["id"], "reject", "not this quarter"
    )

    assert decided.status_code == 200, decided.text
    assert decided.json()["decision_reason"] == "not this quarter"
    reloaded = _status(client, scope, run["id"])
    assert reloaded["status"] == "paused"
    assert reloaded["pause_reason"] == "approval_rejected"
    assert stand_in.requests == []


async def test_a_rejection_with_no_reason_is_refused(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """The person whose Run stopped is not the person who stopped it."""
    await _stopped_run(client, scope, engine, session_for, api, proxy)

    refused = _decide(client, scope, _pending(client, scope)[0]["id"], "reject")

    assert refused.status_code == 422
    assert refused.json()["code"] == "approval_reason_required"


async def test_an_approval_is_decided_once(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """§16.3: a management override is a new governance approval with its
    reasons written down, never a rewrite of the record somebody else made."""
    await _stopped_run(client, scope, engine, session_for, api, proxy)
    approval_id = _pending(client, scope)[0]["id"]
    _decide(client, scope, approval_id, "approve")

    again = _decide(client, scope, approval_id, "reject", "changed my mind")

    assert again.status_code == 409
    assert again.json()["code"] == "approval_already_decided"


async def test_the_run_records_that_it_was_asked_and_answered(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    run, _ = await _stopped_run(client, scope, engine, session_for, api, proxy)
    _decide(client, scope, _pending(client, scope)[0]["id"], "approve")

    kinds = [item["type"] for item in await _events(engine, run["id"])]

    assert "run_approval_requested" in kinds
    assert "run_approval_approved" in kinds


async def test_a_preauthorized_write_never_asks(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """A workspace administrator approved this narrow scope at publish. That is
    the whole of what the choice means, and this is the test that says the
    runtime honours it rather than asking anyway."""
    stand_in, url = api
    approve_host(client, scope, "127.0.0.1")
    version_id = register_tool(client, scope, url)
    session_id = session_for(_agent(client, scope, version_id, "preauthorized"))
    run = ask(client, scope, session_id, "http.orders.createOrder")

    await worker(engine, scope["X-Workspace-Id"], proxy).run_once()

    assert stand_in.methods == ["POST"]
    assert _pending(client, scope) == []
    assert _status(client, scope, run["id"])["status"] == "completed"


async def test_a_disabled_write_never_asks_and_never_sends(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    stand_in, url = api
    approve_host(client, scope, "127.0.0.1")
    version_id = register_tool(client, scope, url)
    session_id = session_for(_agent(client, scope, version_id, "disabled"))
    run = ask(client, scope, session_id, "http.orders.createOrder")

    await worker(engine, scope["X-Workspace-Id"], proxy).run_once()

    assert stand_in.requests == []
    assert _pending(client, scope) == []
    assert _status(client, scope, run["id"])["status"] == "completed"


async def test_an_unanswered_approval_expires_and_pauses_the_run(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """A Run in `waiting_approval` holds no lease and no container, so nothing
    but the scheduler is watching its clock. Without this sweep a question
    nobody answered would keep a Session's head forever.

    The deadline is moved rather than waited out: what is under test is the
    sweep, not the passage of time."""
    run, stand_in = await _stopped_run(client, scope, engine, session_for, api, proxy)
    approval_id = _pending(client, scope)[0]["id"]
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE approvals SET expires_at = now() - interval '1 hour'"),
        )

    await _scheduler(engine).run_once()

    reloaded = _status(client, scope, run["id"])
    assert reloaded["status"] == "paused"
    assert reloaded["pause_reason"] == "approval_expired"
    assert stand_in.requests == []
    assert _pending(client, scope) == []
    late = _decide(client, scope, approval_id, "approve")
    # Answered after it ran out. Honouring it late would resume work whose
    # context nobody has looked at since.
    assert late.status_code == 409
    assert late.json()["code"] == "approval_already_decided"


async def test_an_expired_approval_pause_recovers_and_keeps_what_it_spent(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """The release gate's rule for the approval pauses.

    An approval that ran out cannot be honoured late — the test above pins
    that, and it is why this pause is not a dead end but a **person's** to
    clear. Resuming puts the Run back in the queue; the call it wanted is asked
    about again, from the same history, rather than proceeding on a decision
    nobody made in time.

    And the counters carry forward. A Run that could clear its accounting by
    letting an approval lapse and being resumed would be a safety valve with a
    waiting-shaped hole in it.
    """
    run, _ = await _stopped_run(client, scope, engine, session_for, api, proxy)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE approvals SET expires_at = now() - interval '1 hour'"),
        )
    await _scheduler(engine).run_once()

    stopped = _status(client, scope, run["id"])
    assert stopped["pause_reason"] == "approval_expired"
    spent_before = stopped["budget"]["consumed_model_calls"]
    assert spent_before > 0, "the Run had already spent the round that asked"
    assert "resume" in stopped["available_actions"]

    resumed = client.post(
        f"/api/v1/runs/{run['id']}/resume",
        headers=scope,
        json={"expected_state_version": stopped["state_version"]},
    )
    assert resumed.status_code == 200, resumed.text

    reloaded = _status(client, scope, run["id"])
    assert reloaded["status"] == "queued"
    # Never from zero: the rounds it already spent are still spent.
    assert reloaded["budget"]["consumed_model_calls"] >= spent_before


async def test_changing_the_arguments_invalidates_the_approval(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """§16.3's invalidation rule, asked of the gate the Worker actually uses.

    A person approved one request. A round that composed a different one gets
    a fresh question rather than the old answer — which is the whole reason the
    approval binds a hash of the normalized call and not a Run id."""
    run, _ = await _stopped_run(client, scope, engine, session_for, api, proxy)
    waiting = _pending(client, scope)[0]
    # The target the Worker actually composed, read back rather than guessed:
    # a hand-built one that happened to differ would make this test pass for
    # the wrong reason.
    target = str(waiting["document"]["target"])
    _decide(client, scope, waiting["id"], "approve")
    gate = SqlApprovalGate(async_sessionmaker(engine, expire_on_commit=False))
    approved = normalize_call(
        "http.orders.createOrder",
        {},
        target=target,
        required_permission="http.orders.write",
    )
    changed = normalize_call(
        "http.orders.createOrder",
        {"force": True},
        target=target,
        required_permission="http.orders.write",
    )

    same = await _ask_gate(gate, run["id"], approved)
    other = await _ask_gate(gate, run["id"], changed)

    assert same.verdict is ApprovalVerdict.APPROVED
    assert other.verdict is ApprovalVerdict.REQUESTED


def _scheduler(engine: AsyncEngine) -> SchedulerRuntime:
    return SchedulerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        notifier=NullWakeUpNotifier(),
        settings=SchedulerSettings(
            max_recovery_attempts=1, event_retention_hours=24, batch_size=50
        ),
    )


async def _ask_gate(
    gate: SqlApprovalGate, run_id: Any, call: NormalizedCall
) -> ApprovalCheck:
    return await gate.check(
        run_id=UUID(str(run_id)),
        approval_type=ApprovalType.GOVERNANCE_APPROVAL,
        tool="http.orders.createOrder",
        call_id="http-1",
        call=call,
        required_permission="http.orders.write",
    )
