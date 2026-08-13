"""Reservations and instances, and the one rule the database has to hold.

`acquire` must refuse a Run that already holds a live Reservation. Checking that
with a read followed by a write is a race two Workers can both win, so the
constraint lives in PostgreSQL and this file proves it does — by asking for the
`IntegrityError` rather than by asking the store politely.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tiny_hermes.sandbox.domain.models import (
    InstanceStatus,
    ReservationStatus,
    SandboxInstance,
)
from tiny_hermes.sandbox.infrastructure.sql_store import SqlSandboxStore

DIGEST = "sha256:" + "a" * 64

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
def sessions(engine: AsyncEngine, empty_database: None) -> Sessions:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def opened(sessions: Sessions) -> AsyncGenerator[SqlSandboxStore]:
    """One store on one committed transaction.

    Committing on the way out is what makes the uniqueness tests meaningful: a
    constraint that only ever sees an open transaction has not been tested.
    """
    async with sessions() as session:
        yield SqlSandboxStore(session)
        await session.commit()


def instance(**overrides: Any) -> SandboxInstance:
    fields: dict[str, Any] = {
        "id": uuid4(),
        "container_id": "c" * 64,
        "image_digest": DIGEST,
        "resource_profile": "default",
        "boot_id": uuid4().hex,
        "status": InstanceStatus.RUNNING,
    }
    fields.update(overrides)
    return SandboxInstance(**fields)


async def test_a_reservation_is_created_active_and_read_back(sessions: Sessions) -> None:
    run_id, workspace_id = uuid4(), uuid4()
    async with opened(sessions) as store:
        made = await store.reserve(
            run_id=run_id, workspace_id=workspace_id, instance=instance()
        )
        found = await store.live_for_run(run_id)

    assert made.status is ReservationStatus.ACTIVE
    assert found is not None
    assert found.id == made.id
    assert found.run_id == run_id
    assert found.idle_expires_at is None


async def test_a_second_live_reservation_for_one_run_is_refused_by_the_database(
    sessions: Sessions,
) -> None:
    """Two Workers can both pass a read-then-write check. Only one can pass this."""
    run_id = uuid4()
    async with opened(sessions) as store:
        await store.reserve(run_id=run_id, workspace_id=uuid4(), instance=instance())

    with pytest.raises(IntegrityError):
        async with opened(sessions) as store:
            await store.reserve(run_id=run_id, workspace_id=uuid4(), instance=instance())


async def test_a_released_reservation_frees_the_run_for_another(
    sessions: Sessions,
) -> None:
    """The uniqueness is over *live* reservations, not over history.

    A Run that finished with one sandbox and was retried is entitled to another,
    which a plain unique index on `run_id` would forbid.
    """
    run_id = uuid4()
    async with opened(sessions) as store:
        first = await store.reserve(
            run_id=run_id, workspace_id=uuid4(), instance=instance()
        )
        await store.release(first.id)
        second = await store.reserve(
            run_id=run_id, workspace_id=uuid4(), instance=instance()
        )

    assert first.id != second.id
    async with opened(sessions) as store:
        live = await store.live_for_run(run_id)
    assert live is not None and live.id == second.id


async def test_a_reservation_moves_active_to_kept_to_released(
    sessions: Sessions,
) -> None:
    until = datetime.now(UTC) + timedelta(minutes=5)
    async with opened(sessions) as store:
        made = await store.reserve(
            run_id=uuid4(), workspace_id=uuid4(), instance=instance()
        )
        kept = await store.keep(made.id, idle_expires_at=until)

    assert kept.status is ReservationStatus.KEPT
    assert kept.idle_expires_at is not None
    assert abs((kept.idle_expires_at - until).total_seconds()) < 1

    async with opened(sessions) as store:
        released = await store.release(kept.id)

    assert released.status is ReservationStatus.RELEASED
    # A released reservation keeps nothing warm, and a stale deadline on it
    # would make the Scheduler's expiry scan claim work that is already done.
    assert released.idle_expires_at is None


async def test_a_reservation_can_be_isolated_with_a_reason(sessions: Sessions) -> None:
    async with opened(sessions) as store:
        made = await store.reserve(
            run_id=uuid4(), workspace_id=uuid4(), instance=instance()
        )
        isolated = await store.isolate(made.id, reason="lease_expired")

    assert isolated.status is ReservationStatus.ISOLATED
    assert isolated.isolation_reason == "lease_expired"


async def test_an_isolated_reservation_still_blocks_a_new_one(
    sessions: Sessions,
) -> None:
    """Isolation means "we are not sure this container is gone".

    Handing the Run a second sandbox while the first may still be running is
    exactly the leak the isolated state exists to prevent.
    """
    run_id = uuid4()
    async with opened(sessions) as store:
        made = await store.reserve(
            run_id=run_id, workspace_id=uuid4(), instance=instance()
        )
        await store.isolate(made.id, reason="destroy_unconfirmed")

    with pytest.raises(IntegrityError):
        async with opened(sessions) as store:
            await store.reserve(run_id=run_id, workspace_id=uuid4(), instance=instance())


async def test_an_instance_records_what_it_is_and_what_state_it_is_in(
    sessions: Sessions,
) -> None:
    async with opened(sessions) as store:
        made = await store.reserve(
            run_id=uuid4(),
            workspace_id=uuid4(),
            instance=instance(container_id="d" * 64, boot_id="boot-1"),
        )
        found = await store.read_instance(made.instance_id)

    assert found is not None
    assert found.container_id == "d" * 64
    assert found.image_digest == DIGEST
    assert found.boot_id == "boot-1"
    assert found.status is InstanceStatus.RUNNING


async def test_an_instance_changes_state_and_is_read_back(sessions: Sessions) -> None:
    async with opened(sessions) as store:
        made = await store.reserve(
            run_id=uuid4(), workspace_id=uuid4(), instance=instance()
        )
        await store.set_instance_status(made.instance_id, InstanceStatus.FROZEN)
        found = await store.read_instance(made.instance_id)

    assert found is not None
    assert found.status is InstanceStatus.FROZEN


async def test_kept_reservations_past_their_deadline_are_findable(
    sessions: Sessions,
) -> None:
    """What the Scheduler's reclamation scan asks for."""
    stale, fresh = uuid4(), uuid4()
    now = datetime.now(UTC)
    async with opened(sessions) as store:
        old = await store.reserve(run_id=stale, workspace_id=uuid4(), instance=instance())
        await store.keep(old.id, idle_expires_at=now - timedelta(seconds=1))
        new = await store.reserve(run_id=fresh, workspace_id=uuid4(), instance=instance())
        await store.keep(new.id, idle_expires_at=now + timedelta(minutes=5))
        expired = await store.expired_keeps(now)

    assert [entry.run_id for entry in expired] == [stale]


async def test_an_isolated_reservation_is_not_in_the_expiry_scan(
    sessions: Sessions,
) -> None:
    """Isolation is not a timer.

    An isolated container needs confirming, not destroying five minutes from
    now, and a deadline left on it would put it in the scan that destroys.
    """
    now = datetime.now(UTC)
    async with opened(sessions) as store:
        made = await store.reserve(
            run_id=uuid4(), workspace_id=uuid4(), instance=instance()
        )
        await store.keep(made.id, idle_expires_at=now - timedelta(seconds=1))
        isolated = await store.isolate(made.id, reason="destroy_unconfirmed")
        expired = await store.expired_keeps(now)

    assert isolated.idle_expires_at is None
    assert expired == []


async def test_sandbox_instances_has_no_host_path_column(engine: AsyncEngine) -> None:
    """Technical design §6.4: 不保存任意宿主机路径.

    A column that could hold a host path is a column somebody eventually puts
    one in, and a stored host path is the first half of an escape. Asserted
    against the real table rather than the model, because a migration can add a
    column the model never mentions.
    """

    def columns(sync: Any) -> list[str]:
        return [str(c["name"]) for c in inspect(sync).get_columns("sandbox_instances")]

    async with engine.connect() as connection:
        found = await connection.run_sync(columns)

    assert set(found) == {
        "id",
        "container_id",
        "image_digest",
        "resource_profile",
        "boot_id",
        "status",
        "created_at",
        "updated_at",
    }


async def test_the_live_reservation_index_is_partial(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'sandbox_reservations' AND indexname = :name"
            ),
            {"name": "uq_sandbox_reservations_live_run"},
        )
        definition = str(result.scalar_one())

    assert "UNIQUE" in definition
    assert "WHERE" in definition


async def test_reading_a_reservation_for_an_unknown_run_answers_nothing(
    sessions: Sessions,
) -> None:
    async with opened(sessions) as store:
        assert await store.live_for_run(UUID(int=0)) is None
