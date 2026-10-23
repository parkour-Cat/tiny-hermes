"""Which Run a Worker takes next, and how many may run at once (v2.21 §1.21.2–3).

Design: docs/superpowers/specs/2026-10-23-claim-limits-design.md. Every test
here claims through `SqlRunStore.claim_head`, the one query every Worker uses,
against rows shaped with SQL: who a session belongs to and how a Run came in
are facts of the data, and the claim must read them from there.
"""

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.store import ClaimRunCommand

from .test_scheduler import FULL

PER_CALLER = 3


@pytest.fixture
def queue(
    published_agent: str,
    session_for: Callable[[str], str],
    submit_run: Callable[[str, str], dict[str, Any]],
) -> Callable[[], UUID]:
    """A queued Run at the head of a Session of its own."""

    def made() -> UUID:
        return UUID(str(submit_run(session_for(published_agent), str(uuid4()))["id"]))

    return made


async def _shape(engine: AsyncEngine, sql: str, **params: Any) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(sql), params)


async def _age(engine: AsyncEngine, run_id: UUID, seconds: int) -> None:
    await _shape(
        engine,
        "UPDATE runs SET queued_at = now() - make_interval(secs => :s) WHERE id = :id",
        s=seconds,
        id=run_id,
    )


async def _owned_by(engine: AsyncEngine, run_id: UUID, caller_type: str, caller_id: UUID) -> None:
    await _shape(
        engine,
        "UPDATE sessions SET caller_type = :t, caller_id = :c "
        "WHERE id = (SELECT session_id FROM runs WHERE id = :id)",
        t=caller_type,
        c=caller_id,
        id=run_id,
    )


async def _claim(engine: AsyncEngine, **overrides: Any) -> UUID | None:
    async with async_sessionmaker(engine, expire_on_commit=False).begin() as session:
        claimed = await SqlRunStore(session).claim_head(
            ClaimRunCommand(
                workspace_id=None,
                worker_id=f"worker-{uuid4().hex[:6]}",
                lease_seconds=60,
                request_id=f"claim-{uuid4().hex[:6]}",
                capabilities=FULL,
                **{"per_caller_default": PER_CALLER, **overrides},
            )
        )
    return None if claimed is None else claimed.run.id


# -- order --------------------------------------------------------------------


async def test_a_run_someone_is_chatting_through_goes_before_an_older_background_one(
    queue: Callable[[], UUID], engine: AsyncEngine
) -> None:
    background = queue()
    chatting = queue()
    await _age(engine, background, 30)
    await _age(engine, chatting, 5)
    await _owned_by(engine, chatting, "end_user", uuid4())

    assert await _claim(engine) == chatting
    assert await _claim(engine) == background


async def test_a_synchronous_completion_goes_before_an_older_background_run(
    queue: Callable[[], UUID], engine: AsyncEngine
) -> None:
    background = queue()
    waiting = queue()
    await _age(engine, background, 30)
    await _shape(
        engine, "UPDATE runs SET delivery_mode = 'chat_completions' WHERE id = :id", id=waiting
    )

    assert await _claim(engine) == waiting


async def test_a_child_run_is_background_even_in_an_end_users_session(
    queue: Callable[[], UUID], engine: AsyncEngine
) -> None:
    """Nobody is at a screen waiting on a delegated piece of work."""
    older = queue()
    child = queue()
    await _age(engine, older, 30)
    await _owned_by(engine, child, "end_user", uuid4())
    await _shape(engine, "UPDATE runs SET parent_run_id = :p WHERE id = :id", p=older, id=child)

    assert await _claim(engine) == older


async def test_a_background_run_that_waited_long_enough_goes_first_again(
    queue: Callable[[], UUID], engine: AsyncEngine
) -> None:
    """Promoted, so an endless stream of chat cannot starve it."""
    starved = queue()
    chatting = queue()
    await _age(engine, starved, 130)
    await _age(engine, chatting, 5)
    await _owned_by(engine, chatting, "end_user", uuid4())

    assert await _claim(engine, promote_after_seconds=120) == starved


async def test_runs_of_one_kind_still_go_in_the_order_they_came(
    queue: Callable[[], UUID], engine: AsyncEngine
) -> None:
    first, second = queue(), queue()
    await _age(engine, first, 20)
    await _age(engine, second, 10)

    assert await _claim(engine) == first


# -- limits -------------------------------------------------------------------


async def test_a_person_with_their_limit_running_waits_while_others_go_ahead(
    queue: Callable[[], UUID], engine: AsyncEngine
) -> None:
    person = uuid4()
    theirs = [queue() for _ in range(PER_CALLER + 1)]
    for position, run_id in enumerate(theirs):
        await _owned_by(engine, run_id, "user", person)
        await _age(engine, run_id, 100 - position)
    someone_else = queue()
    await _owned_by(engine, someone_else, "user", uuid4())

    claimed = [await _claim(engine) for _ in range(PER_CALLER)]
    assert claimed == theirs[:PER_CALLER]

    assert await _claim(engine) == someone_else
    assert await _claim(engine) is None, "their fourth waits; it is not refused"


async def test_when_one_of_theirs_finishes_the_next_one_goes(
    queue: Callable[[], UUID], engine: AsyncEngine
) -> None:
    person = uuid4()
    theirs = [queue() for _ in range(PER_CALLER + 1)]
    for position, run_id in enumerate(theirs):
        await _owned_by(engine, run_id, "end_user", person)
        await _age(engine, run_id, 100 - position)
    for _ in range(PER_CALLER):
        await _claim(engine)
    await _shape(
        engine,
        "UPDATE runs SET status = 'completed', finished_at = now() WHERE id = :id",
        id=theirs[0],
    )

    assert await _claim(engine) == theirs[PER_CALLER]


async def test_a_service_account_is_not_held_to_the_per_person_limit(
    queue: Callable[[], UUID], engine: AsyncEngine
) -> None:
    """An integration is many people behind one key."""
    system = uuid4()
    runs = [queue() for _ in range(PER_CALLER + 1)]
    for run_id in runs:
        await _owned_by(engine, run_id, "service_account", system)

    claimed = [await _claim(engine) for _ in runs]

    assert None not in claimed


async def test_a_workspace_limit_holds_everyone_in_it(
    queue: Callable[[], UUID], engine: AsyncEngine, workspace_id: str
) -> None:
    runs = [queue() for _ in range(3)]
    for run_id in runs:
        await _owned_by(engine, run_id, "service_account", uuid4())
    await _shape(
        engine, "UPDATE workspaces SET max_running_runs = 2 WHERE id = :id", id=UUID(workspace_id)
    )

    assert await _claim(engine) is not None
    assert await _claim(engine) is not None
    assert await _claim(engine) is None


async def test_a_workspace_may_set_its_own_per_person_limit(
    queue: Callable[[], UUID], engine: AsyncEngine, workspace_id: str
) -> None:
    person = uuid4()
    runs = [queue() for _ in range(2)]
    for run_id in runs:
        await _owned_by(engine, run_id, "user", person)
    await _shape(
        engine,
        "UPDATE workspaces SET max_running_runs_per_caller = 1 WHERE id = :id",
        id=UUID(workspace_id),
    )

    assert await _claim(engine) is not None
    assert await _claim(engine) is None


async def test_workers_claiming_at_once_do_not_go_over_a_persons_limit(
    queue: Callable[[], UUID], engine: AsyncEngine
) -> None:
    """Each claimer counts before it takes; without a lock between counting and
    taking, several would each count two and each take a third."""
    person = uuid4()
    runs = [queue() for _ in range(PER_CALLER * 3)]
    for run_id in runs:
        await _owned_by(engine, run_id, "user", person)

    claimed = await asyncio.gather(*(_claim(engine) for _ in runs))

    assert len([run_id for run_id in claimed if run_id is not None]) == PER_CALLER
    async with engine.connect() as connection:
        running = await connection.scalar(
            text("SELECT count(*) FROM runs WHERE status = 'running'")
        )
    assert running == PER_CALLER
