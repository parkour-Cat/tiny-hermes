"""让位这条路要真的走通：Run 结束、队首让出、排队那条真的跑起来。

断言停在 `decide_after_round` 的返回值上是不够的——让位的全部意义是后面那条
消息得到处理，而那要跨过 Run 终态、队首推进和 Worker 领取三道关。这里用两次
`WorkerRuntime.run_once`（`worker` fixture 包的那个）而不是一次：第一次只证明
「让位的 Run 真的结束了、队首真的往后挪了」，头顶让出的 Session 不会自己把排
队的 Run 领走——领走要靠 Worker 下一次认领，`claim_head` 只在被调用时才查。
`goal_preempted` 用真实的读路径核对：`SqlRunStore.get_run` 就是
`RunResponse.from_domain` 用来把 checkpoint 变成 API 里 `goal.preempted` 的那条
路径，绕过它——比如直接查 `runs` 表——只会证明写对了，不证明有人读得到。
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.domain.models import RunCapabilities, RunSnapshot
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore

from ..conftest import VALID_SPEC
from .test_worker_goal import ScriptedSandbox, claims_done
from .test_worker_tools import Recording, drive

FULL = RunCapabilities(can_control=True, can_retry=True)

#: The verification command every agent in this file declares, and the one
#: `ScriptedSandbox` is told to always fail. A claim the platform never
#: verifies is a `done` on the first round — this file needs `continue` on
#: the first round, every time, so the preemption branch is the one actually
#: exercised rather than an ordinary completion racing it.
VERIFY = "pytest -q"


def _agent_that_never_verifies(client: TestClient, scope: dict[str, str], *, rounds: int) -> str:
    """An Agent whose declared check never passes, so every judged round is
    `continue` — the one outcome `decide_after_round` will still trade away
    for `user_waiting`. `rounds` caps the shared budget, which is what ends
    the lone-Run case: nobody preempts it, so the budget valve has to.
    """
    alias = f"preempt-{uuid4().hex[:8]}"
    agent = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Preempt", "alias": alias}
    ).json()
    spec: dict[str, Any] = {
        **VALID_SPEC,
        "tools": ["shell.exec"],
        "limits": {**VALID_SPEC["limits"], "max_model_calls": rounds},
        "completion": {"verification_command": VERIFY},
    }
    draft = client.put(
        f"/api/v1/agents/{agent['id']}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": spec},
    ).json()
    published = client.post(
        f"/api/v1/agents/{agent['id']}/publish",
        headers=scope,
        json={"expected_revision": draft["revision"]},
    )
    assert published.status_code in (200, 201), published.text
    return str(agent["id"])


def _new_session(client: TestClient, scope: dict[str, str], agent_id: str) -> str:
    return str(
        client.post("/api/v1/sessions", headers=scope, json={"agent_id": agent_id}).json()["id"]
    )


def _submit_into(client: TestClient, scope: dict[str, str], session_id: str, key: str) -> str:
    response = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": key},
        json={"session_id": session_id, "input": f"message {key}"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _run_row(engine: AsyncEngine, run_id: UUID) -> Row[Any]:
    """`id` alone, but as a `Row` rather than the bare `UUID` in scope for
    submitting it: the tests below read `.id` off what this file's fixtures
    hand them, matching `test_waiting_run.py`'s fixtures next to it.
    """
    async with engine.connect() as connection:
        return (
            await connection.execute(
                text("SELECT id FROM runs WHERE id = :id"), {"id": run_id}
            )
        ).one()


class _RunReader:
    """`SqlRunStore.get_run`, fixed to the one workspace and full
    capabilities every Run in this file belongs to.

    This is the same call `RunResponse.from_domain` makes to answer
    `GET /api/v1/runs/{id}` — going through it, rather than a raw `SELECT`
    against `runs`, is what makes a passing assertion here mean the fact is
    reachable through the platform's own read path, not just present in a
    column.
    """

    def __init__(self, engine: AsyncEngine, workspace_id: UUID) -> None:
        self._engine = engine
        self._workspace_id = workspace_id

    async def read_run(self, run_id: UUID) -> RunSnapshot:
        factory = async_sessionmaker(self._engine, expire_on_commit=False)
        async with factory() as session:
            snapshot = await SqlRunStore(session).get_run(self._workspace_id, run_id, FULL)
        assert snapshot is not None
        return snapshot


@pytest.fixture
def store(engine: AsyncEngine, workspace_id: str) -> _RunReader:
    return _RunReader(engine, UUID(workspace_id))


class _Worker:
    """One call to the real Worker entry point, `WorkerRuntime.run_once`
    (wrapped the way every other test in this directory calls it — see
    `drive` in `test_worker_tools.py`), with a model and sandbox fixed for
    the whole file: claim done, get contradicted by a verification that never
    passes. What differs test to test is not how the model answers but
    whether anyone is queued behind the Run it answers for.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def run_one_slice(self) -> None:
        await drive(
            self._engine,
            Recording(claims_done("all done")),
            ScriptedSandbox(failing=(VERIFY,)),
        )


@pytest.fixture
def worker(engine: AsyncEngine) -> _Worker:
    return _Worker(engine)


@pytest.fixture
async def session_with_a_continuing_run_and_a_queued_message(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine
) -> AsyncIterator[tuple[UUID, Row[Any], Row[Any]]]:
    # Generous budget: preemption has to win on the very first round, so
    # nothing here should depend on how many rounds the agent is allowed.
    agent = _agent_that_never_verifies(client, scope, rounds=4)
    session_id = _new_session(client, scope, agent)
    running_id = UUID(_submit_into(client, scope, session_id, "head"))
    queued_id = UUID(_submit_into(client, scope, session_id, "queued"))
    running = await _run_row(engine, running_id)
    queued = await _run_row(engine, queued_id)
    yield UUID(session_id), running, queued


@pytest.fixture
async def session_with_a_continuing_run_alone(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine
) -> AsyncIterator[tuple[UUID, Row[Any]]]:
    # Tight budget: with nobody to preempt it for, this Run's only way to stop
    # inside one slice is the budget valve, and it should take few rounds to
    # get there rather than running until `max_slice_seconds`.
    agent = _agent_that_never_verifies(client, scope, rounds=2)
    session_id = _new_session(client, scope, agent)
    running_id = UUID(_submit_into(client, scope, session_id, "head"))
    running = await _run_row(engine, running_id)
    yield UUID(session_id), running


async def test_a_waiting_message_actually_runs_after_the_preemption(
    worker: _Worker,
    store: _RunReader,
    session_with_a_continuing_run_and_a_queued_message: tuple[UUID, Row[Any], Row[Any]],
) -> None:
    _session_id, running, queued = session_with_a_continuing_run_and_a_queued_message

    # First slice: the head Run's own round judges `continue`, finds the
    # queued message behind it, and gives up the head instead of looping.
    await worker.run_one_slice()
    assert (await store.read_run(running.id)).state.value == "completed"

    # Giving up the head only makes the queued Run claimable — nothing
    # claims it until a Worker asks again. Without this second slice, the
    # first assertion above would still pass on a platform where the queued
    # message never runs at all, which is exactly the bug this task exists
    # to catch.
    await worker.run_one_slice()
    assert (await store.read_run(queued.id)).state.value != "queued"


async def test_the_preempted_run_says_why_it_ended(
    worker: _Worker,
    store: _RunReader,
    session_with_a_continuing_run_and_a_queued_message: tuple[UUID, Row[Any], Row[Any]],
) -> None:
    _session_id, running, _queued = session_with_a_continuing_run_and_a_queued_message

    await worker.run_one_slice()

    snapshot = await store.read_run(running.id)
    assert snapshot.goal_preempted is True
    assert snapshot.document()["goal"]["preempted"] is True


async def test_a_run_with_nobody_waiting_keeps_going(
    worker: _Worker,
    store: _RunReader,
    session_with_a_continuing_run_alone: tuple[UUID, Row[Any]],
) -> None:
    _session_id, running = session_with_a_continuing_run_alone

    await worker.run_one_slice()

    assert (await store.read_run(running.id)).state.value != "completed"
