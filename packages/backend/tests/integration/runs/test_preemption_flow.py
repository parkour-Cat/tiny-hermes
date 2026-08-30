"""让位这条路要真的走通：Run 结束、队首让出、排队那条真的跑起来。

断言停在 `decide_after_round` 的返回值上是不够的——让位的全部意义是后面那条
消息得到处理，而那要跨过 Run 终态、队首推进和 Worker 领取三道关。这里用两次
`WorkerRuntime.run_once`（`worker` fixture 包的那个）而不是一次：第一次只证明
「让位的 Run 真的结束了、队首真的往后挪了」，头顶让出的 Session 不会自己把排
队的 Run 领走——领走要靠 Worker 下一次认领，`claim_head` 只在被调用时才查。
`goal_preempted` 用真实的读路径核对：`SqlRunStore.get_run` 就是
`RunResponse.from_domain` 用来把 checkpoint 变成 API 里 `goal.preempted` 的那条
路径，绕过它——比如直接查 `runs` 表——只会证明写对了，不证明有人读得到。

v2.9.1 把 §12.1 的判据收窄成「在我开始之后才到」（见 spec §12.1、
`SqlRunStore.has_waiting_run`）。`test_a_message_arriving_mid_round_preempts_the_run`
和 `test_a_message_already_queued_before_start_does_not_preempt` 是这条收窄的
对照组：两者的 Session 形状完全一样，唯一的差别是第二条消息相对头顶 Run
`started_at` 的时间点——mid-round 用 `_SubmittingModel`
在模型被调用（也就是 claim、`started_at` 落定之后）那一刻才提交它，而
already-queued 在 Worker 认领之前就把它提交好。
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
from tiny_hermes.runs.ports.model import ModelRequest, ModelResponse

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

    async def run_one_slice_while_a_message_arrives(
        self, client: TestClient, scope: dict[str, str], session_id: str, key: str
    ) -> None:
        """The mid-round case: the second message is submitted only once the
        Worker has already claimed the head Run and is asking the model for
        its round — see `_SubmittingModel`.
        """
        await drive(
            self._engine,
            _SubmittingModel(
                Recording(claims_done("all done")),
                client=client,
                scope=scope,
                session_id=session_id,
                key=key,
            ),
            ScriptedSandbox(failing=(VERIFY,)),
        )


class _SubmittingModel:
    """Answers from a script, and submits one message into the Session the
    instant its round begins.

    `WorkerRuntime` claims the Run (setting `started_at`) before it ever calls
    a model — `complete` is the one hook this test can use to inject a
    message and be sure its `created_at` lands after that claim, which is
    exactly the ordering `test_a_message_arriving_mid_round_preempts_the_run`
    needs to prove: not "queued behind the head Run" but "arrived after the
    head Run started".
    """

    def __init__(
        self,
        inner: Recording,
        *,
        client: TestClient,
        scope: dict[str, str],
        session_id: str,
        key: str,
    ) -> None:
        self._inner = inner
        self._client = client
        self._scope = scope
        self._session_id = session_id
        self._key = key
        self._submitted = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self._submitted:
            self._submitted = True
            _submit_into(self._client, self._scope, self._session_id, self._key)
        return await self._inner.complete(request)


@pytest.fixture
def worker(engine: AsyncEngine) -> _Worker:
    return _Worker(engine)


@pytest.fixture
async def session_with_a_continuing_run(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine
) -> AsyncIterator[tuple[str, Row[Any]]]:
    """Just the head Run — nothing else exists in the Session yet. What
    happens next (a message arriving mid-round, one already queued, or
    nobody at all) is each test's own concern, added on top of this.
    """
    # Generous budget: preemption has to win on the very first round, so
    # nothing here should depend on how many rounds the agent is allowed.
    agent = _agent_that_never_verifies(client, scope, rounds=4)
    session_id = _new_session(client, scope, agent)
    running_id = UUID(_submit_into(client, scope, session_id, "head"))
    running = await _run_row(engine, running_id)
    yield session_id, running


@pytest.fixture
async def session_with_a_message_already_queued_before_start(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine
) -> AsyncIterator[tuple[UUID, Row[Any], Row[Any]]]:
    """The burst case: both messages exist before a Worker ever claims the
    head Run. §12.1's v2.9.1 wording says this is not preemption — see the
    module docstring for the contrast with the mid-round fixture.
    """
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


async def test_a_message_arriving_mid_round_preempts_the_run(
    worker: _Worker,
    store: _RunReader,
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_with_a_continuing_run: tuple[str, Row[Any]],
) -> None:
    session_id, running = session_with_a_continuing_run

    # First slice: the head Run's own round judges `continue`; a message
    # arrives while that round is in flight (see `_SubmittingModel`), and the
    # Run gives up the head instead of looping.
    await worker.run_one_slice_while_a_message_arrives(client, scope, session_id, "queued")
    assert (await store.read_run(running.id)).state.value == "completed"

    async with engine.connect() as connection:
        queued_id = (
            await connection.execute(
                text(
                    "SELECT id FROM runs WHERE session_id = :session AND id != :head"
                ),
                {"session": UUID(session_id), "head": running.id},
            )
        ).scalar_one()

    # Giving up the head only makes the queued Run claimable — nothing
    # claims it until a Worker asks again. Without this second slice, the
    # first assertion above would still pass on a platform where the queued
    # message never runs at all, which is exactly the bug this task exists
    # to catch.
    await worker.run_one_slice()
    assert (await store.read_run(queued_id)).state.value != "queued"


async def test_the_preempted_run_says_why_it_ended(
    worker: _Worker,
    store: _RunReader,
    client: TestClient,
    scope: dict[str, str],
    session_with_a_continuing_run: tuple[str, Row[Any]],
) -> None:
    session_id, running = session_with_a_continuing_run

    await worker.run_one_slice_while_a_message_arrives(client, scope, session_id, "queued")

    snapshot = await store.read_run(running.id)
    assert snapshot.goal_preempted is True
    assert snapshot.document()["goal"]["preempted"] is True


async def test_the_preempted_run_says_why_on_its_timeline_too(
    worker: _Worker,
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_with_a_continuing_run: tuple[str, Row[Any]],
) -> None:
    """`document()` is not the only surface a reader has: the SSE timeline
    replays `goal_verdict` events independently, and a payload that only
    ever said `{"round", "outcome", "unmet"}` left every past round on that
    timeline looking like an ordinary `continue` — indistinguishable from
    one that was about to run its next round on its own.
    """
    session_id, running = session_with_a_continuing_run

    await worker.run_one_slice_while_a_message_arrives(client, scope, session_id, "queued")

    async with engine.connect() as connection:
        payload = (
            await connection.execute(
                text(
                    "SELECT payload FROM run_events WHERE run_id = :run "
                    "AND event_type = 'goal_verdict' ORDER BY sequence DESC LIMIT 1"
                ),
                {"run": running.id},
            )
        ).scalar_one()
    assert payload["outcome"] == "continue"
    assert payload["preempted"] is True


async def test_a_message_already_queued_before_start_does_not_preempt(
    worker: _Worker,
    store: _RunReader,
    session_with_a_message_already_queued_before_start: tuple[UUID, Row[Any], Row[Any]],
) -> None:
    """The distinction v2.9.1 exists to draw: a message that queued before
    the head Run ever started is not the mid-run interruption §12.1 targets,
    even though something genuinely sits behind the head Run right now.
    """
    _session_id, running, _queued = session_with_a_message_already_queued_before_start

    await worker.run_one_slice()

    assert (await store.read_run(running.id)).state.value != "completed"


async def test_a_run_with_nobody_waiting_keeps_going(
    worker: _Worker,
    store: _RunReader,
    session_with_a_continuing_run_alone: tuple[UUID, Row[Any]],
) -> None:
    _session_id, running = session_with_a_continuing_run_alone

    await worker.run_one_slice()

    assert (await store.read_run(running.id)).state.value != "completed"
