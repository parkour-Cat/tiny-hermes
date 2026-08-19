"""The parent's wait: what it gives up, what wakes it, and what it is told.

§13's tenth and eleventh clauses. These are separated from `test_child_runs.py`
because that suite is about children existing and this one is about the parent
**not running** while they do — which is a different property and the one with
the expensive failure modes.

Three of them are worth naming.

A parent that waited while holding its lease would be a Worker slot and a
container tied up doing nothing, for as long as its children take. §13 says the
lease is released and the sandbox destroyed, and the first test asserts the rows
rather than the intent.

A parent woken by `any` leaves siblings running. They are still spending the
root budget on an answer nobody will read, so §13 makes cancelling them the
default — and a default nobody can observe is a default that quietly stops
being true.

A parent whose children all failed must be **told**, not failed. It may be able
to do the work itself, and a platform that turned somebody else's failure into
its own would take that decision away from it.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.scheduler import SchedulerRuntime, SchedulerSettings
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.infrastructure.sql_children import SqlChildRuns

from ..conftest import VALID_SPEC


def _worker(engine: AsyncEngine, workspace_id: str, name: str) -> WorkerRuntime:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return WorkerRuntime(
        session_factory=sessions,
        model=DeterministicModelProvider(delay_ms=0),
        notifier=NullWakeUpNotifier(),
        children=SqlChildRuns(sessions),
        settings=WorkerSettings(
            worker_id=name,
            lease_seconds=30,
            max_slice_seconds=30,
            idle_poll_seconds=1,
            workspace_id=UUID(workspace_id),
        ),
    )


def _scheduler(engine: AsyncEngine) -> SchedulerRuntime:
    return SchedulerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        notifier=NullWakeUpNotifier(),
        settings=SchedulerSettings(max_recovery_attempts=3, event_retention_hours=24),
    )


def _publish(
    client: TestClient, scope: dict[str, str], alias: str, spec: dict[str, Any]
) -> str:
    agent_id = str(
        client.post(
            "/api/v1/agents", headers=scope, json={"name": alias.title(), "alias": alias}
        ).json()["id"]
    )
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


def _child(
    client: TestClient,
    scope: dict[str, str],
    alias: str,
    scenario: str,
    tools: list[str] | None = None,
) -> None:
    _publish(
        client,
        scope,
        alias,
        {
            **VALID_SPEC,
            "model_policy": {"provider": "deterministic", "scenario": scenario},
            **({} if tools is None else {"tools": tools}),
        },
    )


def _parent(
    client: TestClient,
    scope: dict[str, str],
    aliases: tuple[str, ...],
    grants: dict[str, list[str]] | None = None,
) -> str:
    """A coordinator bound to these children, granting each the tools named.

    `grants` is not a convenience. §13's sixth clause makes a child's
    permission the intersection of its parent's and the delegation's, so a face
    nobody named grants nothing — a child that needs `platform.wait` has to be
    given it here even though its own Version bound it. The parent must hold
    the tool too, which is why it appears in the coordinator's own `tools`.
    """
    granted = grants or {}
    every = sorted({tool for tools in granted.values() for tool in tools})
    return _publish(
        client,
        scope,
        "coordinator",
        {
            **VALID_SPEC,
            "model_policy": {"provider": "deterministic", "scenario": "delegate_once"},
            # Publishing refuses a delegation offering what the parent does not
            # itself hold, so the coordinator carries every tool it hands down.
            "tools": ["agent.delegate", *every],
            "delegation": {
                "max_parallel": 4,
                "children": [
                    {"alias": alias, "tools": granted.get(alias, [])}
                    for alias in aliases
                ],
            },
        },
    )


async def _rows(engine: AsyncEngine, sql: str, **params: object) -> list[Any]:
    async with engine.connect() as connection:
        return list((await connection.execute(text(sql), params)).all())


async def _work(engine: AsyncEngine, workspace_id: str, rounds: int = 6) -> None:
    """Advance the Workers only. Nothing here settles a wait."""
    workers = (
        _worker(engine, workspace_id, "worker-a"),
        _worker(engine, workspace_id, "worker-b"),
    )
    for _ in range(rounds):
        advanced = await asyncio.gather(*(worker.run_once() for worker in workers))
        if not any(advanced):
            return


def _start(
    client: TestClient,
    scope: dict[str, str],
    session_id: str,
    key: str,
    text_input: str,
) -> str:
    created = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": key},
        json={"session_id": session_id, "input": text_input},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


@pytest.fixture
def two_children(client: TestClient, scope: dict[str, str]) -> str:
    _child(client, scope, "reader", "complete")
    _child(client, scope, "checker", "complete")
    return _parent(client, scope, ("reader", "checker"))


async def test_a_waiting_parent_holds_no_lease_and_no_sandbox(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    two_children: str,
) -> None:
    """§13's tenth clause, asserted on the rows it is about.

    Only the Workers run here, so the parent is caught mid-wait rather than
    after it. It is in `waiting_external` on `child_runs`, with a policy and a
    deadline — and it holds neither of the two scarce things a running Run
    holds. A parent that kept them would tie up a Worker slot and a container
    for as long as its children take, which is the cost the whole state exists
    to avoid.
    """
    workspace_id = scope["X-Workspace-Id"]
    parent_run = _start(
        client, scope, session_for(two_children), "wait-holds", "reader,checker"
    )

    await _work(engine, workspace_id)

    parent = (
        await _rows(
            engine,
            "SELECT status, wait_kind, wait_policy, wait_deadline_at "
            "FROM runs WHERE id = :p",
            p=UUID(parent_run),
        )
    )[0]
    assert parent.status == "waiting_external"
    assert parent.wait_kind == "child_runs"
    assert parent.wait_policy == "all"
    assert parent.wait_deadline_at is not None

    held = await _rows(
        engine,
        "SELECT id FROM worker_leases WHERE run_id = :p AND released_at IS NULL",
        p=UUID(parent_run),
    )
    assert held == [], "a waiting parent must not hold its lease"

    boxes = await _rows(
        engine,
        "SELECT id FROM sandbox_reservations WHERE run_id = :p "
        "AND status IN ('reserved', 'kept')",
        p=UUID(parent_run),
    )
    assert boxes == [], "a waiting parent must not hold a sandbox"


async def test_the_parent_does_not_wake_itself(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    two_children: str,
) -> None:
    """Stated as a test because it is the thing a reader will assume otherwise.

    Workers run to exhaustion — the children finish — and the parent is still
    waiting. Nothing in the execution path settles a wait; the Scheduler does,
    and until it ticks the parent stays exactly where it is.
    """
    workspace_id = scope["X-Workspace-Id"]
    parent_run = _start(
        client, scope, session_for(two_children), "wait-self", "reader,checker"
    )

    await _work(engine, workspace_id)

    children = await _rows(
        engine, "SELECT status FROM runs WHERE parent_run_id = :p", p=UUID(parent_run)
    )
    assert [row.status for row in children] == ["completed", "completed"]
    still = await _rows(engine, "SELECT status FROM runs WHERE id = :p", p=UUID(parent_run))
    assert still[0].status == "waiting_external"

    await _scheduler(engine).run_once()

    woken = await _rows(engine, "SELECT status FROM runs WHERE id = :p", p=UUID(parent_run))
    assert woken[0].status == "queued"


async def test_the_parent_is_handed_the_results_and_not_the_transcripts(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    two_children: str,
) -> None:
    """§13's seventh clause, on the parent's own conversation.

    The delivered turn names both children and carries what each reported. What
    it does not carry is the instruction each was given or the turns it took to
    get there — those stay in the child's Session, where a person can read them
    and the parent's context planner never has to trim somebody else's work.
    """
    workspace_id = scope["X-Workspace-Id"]
    parent_session = session_for(two_children)
    parent_run = _start(client, scope, parent_session, "wait-results", "reader,checker")

    await _work(engine, workspace_id)
    await _scheduler(engine).run_once()

    delivered = await _rows(
        engine,
        "SELECT content FROM session_messages WHERE session_id = :s "
        "ORDER BY sequence DESC LIMIT 1",
        s=UUID(parent_session),
    )
    body = str(delivered[0].content)
    children = await _rows(
        engine, "SELECT id FROM runs WHERE parent_run_id = :p", p=UUID(parent_run)
    )
    assert len(children) == 2
    for child in children:
        assert str(child.id) in body
    # The instruction each child was given is in the child's Session and
    # nowhere in the parent's.
    assert "Do the reader part." not in body

    stamped = await _rows(
        engine,
        "SELECT result_delivered_at FROM runs WHERE parent_run_id = :p",
        p=UUID(parent_run),
    )
    assert all(row.result_delivered_at is not None for row in stamped)


async def test_a_result_is_delivered_once_however_often_the_sweep_runs(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    two_children: str,
) -> None:
    """The roadmap's last exit criterion, driven by the retry it is about.

    The Scheduler is idempotent by design and runs on every tick, so "delivered
    once" has to survive the sweep seeing the same children again and again.
    `result_delivered_at` is what makes it survive: the second sweep finds
    nothing undelivered and appends nothing.

    A parent that is temporarily unavailable is the same situation from the
    other side — the sweep before the children finish delivers nothing and the
    later one delivers everything, exactly once between them.
    """
    workspace_id = scope["X-Workspace-Id"]
    parent_session = session_for(two_children)
    _start(client, scope, parent_session, "wait-once", "reader,checker")

    scheduler = _scheduler(engine)
    # A sweep while the parent is still working and the children do not exist.
    await scheduler.run_once()
    await _work(engine, workspace_id)
    # And now several, of which only the first can find anything to hand over.
    for _ in range(4):
        await scheduler.run_once()

    # Matched on the platform's authorship rather than on a phrase: the tool
    # result the parent already holds describes the same wait in similar words,
    # and a text match would have counted it and passed for the wrong reason.
    handed = await _rows(
        engine,
        "SELECT count(*) AS n FROM session_messages WHERE session_id = :s "
        "AND content->>'author' = 'platform'",
        s=UUID(parent_session),
    )
    assert handed[0].n == 1


async def test_any_wakes_on_the_first_success_and_cancels_the_rest(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """§13's default, and the reason it is the default.

    One child completes and one is still going. Under `any` the parent is woken
    on the success and the sibling is cancelled — it was spending the root
    budget on an answer nobody was going to read.
    """
    workspace_id = scope["X-Workspace-Id"]
    _child(client, scope, "quick", "complete")
    # This one waits rather than finishing, so at the moment `quick` succeeds it
    # is genuinely still going — the situation `any` is about.
    _child(client, scope, "slow", "wait_once", tools=["platform.wait"])
    parent = _parent(client, scope, ("quick", "slow"), {"slow": ["platform.wait"]})
    # The policy comes from the delegating call, so it has to be asked for
    # there. Setting the column beforehand would be overwritten by the round
    # that starts the wait — which is the correct behaviour and was worth
    # finding out this way.
    parent_run = _start(
        client, scope, session_for(parent), "wait-any", "any:quick,slow"
    )

    await _work(engine, workspace_id)
    await _scheduler(engine).run_once()

    states = {
        row.alias: row.status
        for row in await _rows(
            engine,
            "SELECT a.alias, r.status FROM runs r "
            "JOIN agent_versions av ON av.id = r.agent_version_id "
            "JOIN agents a ON a.id = av.agent_id WHERE r.parent_run_id = :p",
            p=UUID(parent_run),
        )
    }
    assert states["quick"] == "completed"
    assert states["slow"] == "cancelled"
    woken = await _rows(engine, "SELECT status FROM runs WHERE id = :p", p=UUID(parent_run))
    assert woken[0].status == "queued"


async def test_children_that_all_failed_wake_the_parent_rather_than_failing_it(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """§13 again: the parent is told, not failed.

    Both children fail. The parent goes back to `queued` holding a summary that
    names what happened, because it may well be able to do the work itself —
    and a platform that turned somebody else's failure into the parent's would
    have taken that decision away from it.
    """
    workspace_id = scope["X-Workspace-Id"]
    _child(client, scope, "reader", "fail_replay_safe")
    _child(client, scope, "checker", "fail_replay_safe")
    parent = _parent(client, scope, ("reader", "checker"))
    parent_session = session_for(parent)
    parent_run = _start(client, scope, parent_session, "wait-fail", "reader,checker")

    await _work(engine, workspace_id)
    await _scheduler(engine).run_once()

    states = await _rows(
        engine, "SELECT status FROM runs WHERE parent_run_id = :p", p=UUID(parent_run)
    )
    assert [row.status for row in states] == ["failed", "failed"]

    woken = await _rows(engine, "SELECT status FROM runs WHERE id = :p", p=UUID(parent_run))
    assert woken[0].status == "queued"

    delivered = await _rows(
        engine,
        "SELECT content FROM session_messages WHERE session_id = :s "
        "ORDER BY sequence DESC LIMIT 1",
        s=UUID(parent_session),
    )
    assert "failed" in str(delivered[0].content)


async def test_a_wait_nobody_answers_becomes_paused_external_timeout(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """The deadline half, which the Scheduler already handled for every kind.

    `_settle_due_waits` treats anything that is not a `timer` as "nobody
    answered", and this asserts that `child_runs` is inside that anything
    rather than beside it. The deadline is pushed into the past instead of
    waiting one out.
    """
    workspace_id = scope["X-Workspace-Id"]
    _child(client, scope, "slow", "wait_once", tools=["platform.wait"])
    parent = _parent(client, scope, ("slow",), {"slow": ["platform.wait"]})
    parent_run = _start(client, scope, session_for(parent), "wait-expiry", "slow")

    await _work(engine, workspace_id)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE runs SET wait_deadline_at = :then WHERE id = :p"),
            {"then": datetime.now(UTC) - timedelta(minutes=5), "p": UUID(parent_run)},
        )

    await _scheduler(engine).run_once()

    parent_row = (
        await _rows(
            engine, "SELECT status, pause_reason FROM runs WHERE id = :p", p=UUID(parent_run)
        )
    )[0]
    assert parent_row.status == "paused"
    assert parent_row.pause_reason == "external_timeout"


async def test_cancelling_a_parent_cancels_the_children_it_is_waiting_on(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """§13's eleventh clause.

    The parent is cancelled while its child is still going. A child that
    outlived it would keep spending the root budget on work whose only reader
    has gone, and nobody would be watching the bill it ran up.
    """
    workspace_id = scope["X-Workspace-Id"]
    _child(client, scope, "slow", "wait_once", tools=["platform.wait"])
    parent = _parent(client, scope, ("slow",), {"slow": ["platform.wait"]})
    parent_run = _start(client, scope, session_for(parent), "wait-cascade", "slow")

    await _work(engine, workspace_id)

    snapshot = client.get(f"/api/v1/runs/{parent_run}", headers=scope).json()
    cancelled = client.post(
        f"/api/v1/runs/{parent_run}/cancel",
        headers=scope,
        json={"expected_state_version": snapshot["state_version"]},
    )
    assert cancelled.status_code == 200, cancelled.text

    await _scheduler(engine).run_once()

    children = await _rows(
        engine, "SELECT status FROM runs WHERE parent_run_id = :p", p=UUID(parent_run)
    )
    assert children, "the parent should have had a child to cancel"
    assert all(row.status == "cancelled" for row in children)


async def test_a_child_loses_a_tool_its_own_version_bound_and_the_delegation_did_not(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """§13's sixth clause where it actually bites: the intersection **narrows**.

    That a child cannot be given more than its parent holds was already true —
    publishing refuses it. This is the other half, and it was the gap: a child
    whose own Version binds `platform.wait` must not be able to use it when the
    delegation named no tools, because its permission is the intersection and
    not its own Version.

    Asserted through behaviour rather than through a scope column. The same
    Agent, the same scenario, two delegations: given the tool it waits, denied
    it the call is refused and it finishes. A column would have said the scope
    was recorded; only this says it was obeyed.
    """
    workspace_id = scope["X-Workspace-Id"]
    _child(client, scope, "slow", "wait_once", tools=["platform.wait"])

    # Named: the child keeps what its Version bound and genuinely waits.
    allowed = _parent(client, scope, ("slow",), {"slow": ["platform.wait"]})
    waiting_run = _start(client, scope, session_for(allowed), "narrow-yes", "slow")
    await _work(engine, workspace_id)
    waited = await _rows(
        engine, "SELECT status FROM runs WHERE parent_run_id = :p", p=UUID(waiting_run)
    )
    assert [row.status for row in waited] == ["waiting_external"]

    # Not named: the same Version, the same call, refused.
    client.delete(f"/api/v1/agents/{allowed}", headers=scope)
    denied = _publish(
        client,
        scope,
        "coordinator2",
        {
            **VALID_SPEC,
            "model_policy": {"provider": "deterministic", "scenario": "delegate_once"},
            "tools": ["agent.delegate", "platform.wait"],
            "delegation": {"max_parallel": 2, "children": [{"alias": "slow"}]},
        },
    )
    quiet_run = _start(client, scope, session_for(denied), "narrow-no", "slow")
    await _work(engine, workspace_id)
    finished = await _rows(
        engine,
        "SELECT status, delegation_scope FROM runs WHERE parent_run_id = :p",
        p=UUID(quiet_run),
    )
    assert len(finished) == 1
    # It ran to a terminal state instead of waiting: the wait it asked for was
    # refused, so there was nothing to hold it.
    assert finished[0].status != "waiting_external"
    scope_document = cast(dict[str, Any], finished[0].delegation_scope or {})
    assert cast(list[str], scope_document.get("tools", [])) == []


async def test_an_external_timeout_pause_recovers_and_the_tree_keeps_its_counters(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """The release gate's rule for the pause M2E's wait produces.

    A `child_runs` wait whose deadline passes becomes
    `paused(external_timeout)` — that much is asserted above. This is the other
    half the gate asks for: somebody resumes it, it runs again, and **the root
    budget carries on from where it was**. A tree that reset its counters by
    timing out and being resumed would be a safety valve anybody can clear by
    waiting.

    The child is left running rather than tidied away, because that is the real
    situation: the deadline passed while somebody else's work was still going,
    and resuming the parent must not depend on the child having finished.
    """
    workspace_id = scope["X-Workspace-Id"]
    _child(client, scope, "slow", "wait_once", tools=["platform.wait"])
    parent = _parent(client, scope, ("slow",), {"slow": ["platform.wait"]})
    parent_run = _start(client, scope, session_for(parent), "timeout-recover", "slow")

    await _work(engine, workspace_id)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE runs SET wait_deadline_at = :then WHERE id = :p"),
            {"then": datetime.now(UTC) - timedelta(minutes=5), "p": UUID(parent_run)},
        )
    await _scheduler(engine).run_once()

    stopped = client.get(f"/api/v1/runs/{parent_run}", headers=scope).json()
    assert stopped["status"] == "paused"
    assert stopped["pause_reason"] == "external_timeout"
    spent_before = stopped["budget"]["consumed_model_calls"]
    assert spent_before > 0, "the parent had already spent a round delegating"
    assert "resume" in stopped["available_actions"]

    resumed = client.post(
        f"/api/v1/runs/{parent_run}/resume",
        headers=scope,
        json={"expected_state_version": stopped["state_version"]},
    )
    assert resumed.status_code == 200, resumed.text

    await _work(engine, workspace_id)

    reloaded = client.get(f"/api/v1/runs/{parent_run}", headers=scope).json()
    assert reloaded["status"] != "paused"
    # Forward from where it was, never from zero. This is the number that would
    # move if a timeout-then-resume handed a tree a fresh budget.
    assert reloaded["budget"]["consumed_model_calls"] >= spent_before
