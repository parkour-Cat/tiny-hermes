"""后面有没有人在等——判定自动续跑该不该让位的那个事实。

v2.9.1 把参照系从 `session_sequence`（位置）收窄成当前 Run 自己的 `started_at`
（时间，§12.1）：判据是「在我开始之后才到」，不是「后面排着」。一段消息如果在
Worker 认领这个 Run 之前就已经排好队，用 `session_sequence` 一样会命中「后面排
着」，但那是排队，不是插话——用户连发三条消息，后两条在第一条开始执行前就已入
队，不该因此各自只跑一轮就被打断。
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore


@pytest.fixture
async def store(engine: AsyncEngine) -> AsyncIterator[SqlRunStore]:
    """A store bound to its own session, the same shape the Worker gets per slice."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        yield SqlRunStore(session)


def _submit(
    client: TestClient, scope: dict[str, str], session_id: str, key: str
) -> dict[str, object]:
    response = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": key},
        json={"session_id": session_id, "input": f"message {key}"},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def _run_row(engine: AsyncEngine, run_id: UUID) -> Row[Any]:
    """`id`/`started_at` for one Run — the column these tests key off of, in
    place of the `session_sequence` the old version of this file used.
    """
    async with engine.connect() as connection:
        return (
            await connection.execute(
                text("SELECT id, started_at FROM runs WHERE id = :id"),
                {"id": run_id},
            )
        ).one()


async def _mark_started(engine: AsyncEngine, run_id: UUID) -> None:
    """Stand in for a Worker's claim: only `started_at` matters to
    `has_waiting_run`, and setting it directly keeps these tests from also
    depending on the claim/lease machinery they are not testing.
    """
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE runs SET status = 'running', started_at = now() WHERE id = :id"),
            {"id": run_id},
        )


@pytest.fixture
async def session_with_one_running_run(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> tuple[UUID, Row[Any]]:
    run = _submit(client, scope, session_id, "only")
    await _mark_started(engine, UUID(str(run["id"])))
    row = await _run_row(engine, UUID(str(run["id"])))
    return UUID(session_id), row


@pytest.fixture
async def session_with_a_message_queued_before_the_run_started(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> tuple[UUID, Row[Any], Row[Any]]:
    """The burst case: both messages exist before a Worker ever claims the
    first one — §566's queue, not a mid-run interruption.
    """
    head = _submit(client, scope, session_id, "head")
    queued = _submit(client, scope, session_id, "queued")
    await _mark_started(engine, UUID(str(head["id"])))
    head_row = await _run_row(engine, UUID(str(head["id"])))
    queued_row = await _run_row(engine, UUID(str(queued["id"])))
    return UUID(session_id), head_row, queued_row


@pytest.fixture
async def session_with_a_message_queued_after_the_run_started(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> tuple[UUID, Row[Any], Row[Any]]:
    """The interruption case: the second message does not exist yet when the
    first Run starts executing — it arrives mid-run.
    """
    head = _submit(client, scope, session_id, "head")
    await _mark_started(engine, UUID(str(head["id"])))
    queued = _submit(client, scope, session_id, "queued")
    head_row = await _run_row(engine, UUID(str(head["id"])))
    queued_row = await _run_row(engine, UUID(str(queued["id"])))
    return UUID(session_id), head_row, queued_row


@pytest.fixture
async def session_with_a_finished_run_behind(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> tuple[UUID, Row[Any], Row[Any]]:
    head = _submit(client, scope, session_id, "head")
    await _mark_started(engine, UUID(str(head["id"])))
    finished = _submit(client, scope, session_id, "finished")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE runs SET status = 'completed', finished_at = now() "
                "WHERE id = :id"
            ),
            {"id": UUID(str(finished["id"]))},
        )
    head_row = await _run_row(engine, UUID(str(head["id"])))
    finished_row = await _run_row(engine, UUID(str(finished["id"])))
    return UUID(session_id), head_row, finished_row


@pytest.fixture
async def session_with_a_paused_sibling_queued_after_start(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> tuple[UUID, Row[Any], Row[Any]]:
    """A sibling that arrived after the head Run started, non-terminal, but
    `paused` rather than `queued`.

    Not something `claim_head` will ever pick up — `_select_claimable` only
    matches `status == 'queued'` — so it stands in for the class of Runs
    `has_waiting_run` must not call "waiting": preempting for one of these
    hands the Session head to something no Worker will claim.
    """
    head = _submit(client, scope, session_id, "head")
    await _mark_started(engine, UUID(str(head["id"])))
    paused = _submit(client, scope, session_id, "paused")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE runs SET status = 'paused', pause_reason = 'manual' "
                "WHERE id = :id"
            ),
            {"id": UUID(str(paused["id"]))},
        )
    head_row = await _run_row(engine, UUID(str(head["id"])))
    paused_row = await _run_row(engine, UUID(str(paused["id"])))
    return UUID(session_id), head_row, paused_row


async def test_a_run_alone_in_its_session_has_nobody_waiting(
    store: SqlRunStore, session_with_one_running_run: tuple[UUID, Row[Any]]
) -> None:
    session_id, run = session_with_one_running_run

    assert await store.has_waiting_run(session_id, run.id, run.started_at) is False


async def test_a_message_queued_before_the_run_started_is_not_waiting(
    store: SqlRunStore,
    session_with_a_message_queued_before_the_run_started: tuple[UUID, Row[Any], Row[Any]],
) -> None:
    """The distinction §12.1 was narrowed to draw: already-queued is not
    mid-run interruption, even though something genuinely sits behind the
    head Run right now.
    """
    session_id, head, _queued = session_with_a_message_queued_before_the_run_started

    assert await store.has_waiting_run(session_id, head.id, head.started_at) is False


async def test_a_message_queued_after_the_run_started_is_waiting(
    store: SqlRunStore,
    session_with_a_message_queued_after_the_run_started: tuple[UUID, Row[Any], Row[Any]],
) -> None:
    session_id, head, _queued = session_with_a_message_queued_after_the_run_started

    assert await store.has_waiting_run(session_id, head.id, head.started_at) is True


async def test_a_terminal_run_behind_it_is_not_waiting(
    store: SqlRunStore,
    session_with_a_finished_run_behind: tuple[UUID, Row[Any], Row[Any]],
) -> None:
    session_id, head, _finished = session_with_a_finished_run_behind

    assert await store.has_waiting_run(session_id, head.id, head.started_at) is False


async def test_a_paused_sibling_queued_after_start_does_not_preempt(
    store: SqlRunStore,
    session_with_a_paused_sibling_queued_after_start: tuple[UUID, Row[Any], Row[Any]],
) -> None:
    """`paused` is not terminal, but it is also not `queued`: preempting for
    it would hand the Session head to a Run `claim_head` will never pick up,
    stalling the Session on nothing. Only a Run a Worker can actually claim
    next counts as "waiting" — see `has_waiting_run`'s docstring.
    """
    session_id, head, _paused = session_with_a_paused_sibling_queued_after_start

    assert await store.has_waiting_run(session_id, head.id, head.started_at) is False


async def test_a_run_ahead_of_it_does_not_count(
    store: SqlRunStore,
    session_with_a_message_queued_after_the_run_started: tuple[UUID, Row[Any], Row[Any]],
) -> None:
    session_id, _head, queued = session_with_a_message_queued_after_the_run_started

    # 从排队那条自己的角度看，它自己还没被 Worker 认领，`started_at` 是 NULL——
    # 没有「我开始之后」这个参照系，也就谈不上有谁排在它后面。
    assert queued.started_at is None
    assert await store.has_waiting_run(session_id, queued.id, queued.started_at) is False


async def test_a_run_with_no_started_at_has_no_frame_of_reference(
    store: SqlRunStore, session_id: str
) -> None:
    """A defensive edge case rather than one the Worker can trigger today:
    `_has_waiting_run` is only ever called on a Run the claim just started,
    so `started_at` is always set by the time it asks. Still, `None` in means
    "no basis to say anyone arrived after me" rather than an error — treating
    it as `True` would prefer a guess over an honest unknown.
    """
    assert await store.has_waiting_run(UUID(session_id), uuid4(), None) is False


@pytest.fixture
async def session_with_a_paused_run_ahead_of_a_later_queued_one(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> tuple[UUID, Row[Any], Row[Any], Row[Any]]:
    """A running; B queued right behind it and then paused (`(QUEUED,
    PAUSE_REQUESTED) → PAUSED` is reachable from the public API); C queued
    after A started.

    `_terminalize` hands the Session head to the *earliest* `session_sequence`
    non-terminal Run when A ends — that is B, not C, regardless of which of
    the two is `queued`. `has_waiting_run` must therefore answer about
    whichever Run would actually become head, not about whether some queued
    Run exists anywhere behind A: B is that Run here, and B is paused, so
    preempting A would hand the head to something `claim_head` never picks
    up. C, further back, stays blocked behind B either way.
    """
    a = _submit(client, scope, session_id, "a")
    await _mark_started(engine, UUID(str(a["id"])))
    b = _submit(client, scope, session_id, "b")
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE runs SET status = 'paused', pause_reason = 'manual' WHERE id = :id"),
            {"id": UUID(str(b["id"]))},
        )
    c = _submit(client, scope, session_id, "c")
    a_row = await _run_row(engine, UUID(str(a["id"])))
    b_row = await _run_row(engine, UUID(str(b["id"])))
    c_row = await _run_row(engine, UUID(str(c["id"])))
    return UUID(session_id), a_row, b_row, c_row


async def test_a_further_back_queued_run_does_not_preempt_when_the_true_successor_is_paused(
    store: SqlRunStore,
    session_with_a_paused_run_ahead_of_a_later_queued_one: tuple[
        UUID, Row[Any], Row[Any], Row[Any]
    ],
) -> None:
    """MAJOR 2: "a queued sibling exists somewhere behind me" is not the same
    fact as "the sibling that will actually become head is claimable" — the
    first is true here (C), the second is false (B is next in line, and B is
    `paused`). Preempting on the first fact alone would truncate A's goal for
    nothing and hand the Session to a Run nobody will claim.
    """
    session_id, a, _b, _c = session_with_a_paused_run_ahead_of_a_later_queued_one

    assert await store.has_waiting_run(session_id, a.id, a.started_at) is False


@pytest.fixture
async def session_with_a_queued_successor_ahead_of_a_message_that_arrived_mid_run(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> tuple[UUID, Row[Any], Row[Any], Row[Any]]:
    """A running; B queued right behind it, created *before* A started (part
    of the same burst — §566, not an interruption); C queued, created
    *after* A started — the mid-run correction §12.1 exists for.

    The successor `_terminalize` would hand the head to is B, and B is
    `queued` — claimable. Whether *B itself* arrived after A started is a
    different question from whether *anyone* did, and C answers the second:
    a message genuinely arrived while A was working, and A must still give
    it up even though the Run that gets the head next is B, not C.
    """
    a = _submit(client, scope, session_id, "a")
    b = _submit(client, scope, session_id, "b")
    await _mark_started(engine, UUID(str(a["id"])))
    c = _submit(client, scope, session_id, "c")
    a_row = await _run_row(engine, UUID(str(a["id"])))
    b_row = await _run_row(engine, UUID(str(b["id"])))
    c_row = await _run_row(engine, UUID(str(c["id"])))
    return UUID(session_id), a_row, b_row, c_row


async def test_a_message_arriving_mid_run_still_preempts_when_the_successor_was_already_queued(
    store: SqlRunStore,
    session_with_a_queued_successor_ahead_of_a_message_that_arrived_mid_run: tuple[
        UUID, Row[Any], Row[Any], Row[Any]
    ],
) -> None:
    """The other half of the fix `test_a_further_back_queued_run_does_not_preempt_...`
    pins: claimability (is the successor `queued`?) and arrival (did *some*
    message show up after I started?) are two separate questions about two
    separate Runs. Requiring the arrival test on the successor specifically
    — B, created before A started — would wrongly say no here and let A grind
    on while C, the actual mid-run correction, waits. §12.1's trigger is an
    existence test over the whole Session, not a property of whichever Run
    happens to be next in line.
    """
    session_id, a, _b, _c = (
        session_with_a_queued_successor_ahead_of_a_message_that_arrived_mid_run
    )

    assert await store.has_waiting_run(session_id, a.id, a.started_at) is True
