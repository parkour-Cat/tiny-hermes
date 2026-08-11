"""Reclaiming containers nobody owns any more.

Two different situations, and the order matters in one of them.

A *kept* reservation is orderly: a Run froze its instance at a slice boundary
and never came back inside the TTL, so the container is idle and can be
destroyed. An *expired lease* is not orderly: the Worker went away while
holding the Run, the container may still be running a command, and the
Reservation must be isolated before anything else happens to it — isolation is
what stops the Run being handed a second sandbox while the first may still be
alive.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tiny_hermes.runs.application.scheduler import SchedulerRuntime, SchedulerSettings
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.sandbox.domain.models import (
    InstanceStatus,
    ReservationStatus,
    SandboxInstance,
)
from tiny_hermes.sandbox.infrastructure.sql_store import SqlSandboxStore

DIGEST = "sha256:" + "f" * 64


@dataclass
class StandInController:
    """The Controller's cleanup surface, and whether it could confirm."""

    fails: bool = False
    cleaned: list[UUID] = field(default_factory=list[UUID])

    async def cleanup(self, *, run_id: UUID, sandbox_id: UUID) -> None:
        del run_id
        if self.fails:
            raise RuntimeError("the daemon did not answer")
        self.cleaned.append(sandbox_id)


def instance() -> SandboxInstance:
    return SandboxInstance(
        id=uuid4(),
        container_id=uuid4().hex,
        image_digest=DIGEST,
        resource_profile="default",
        boot_id=uuid4().hex,
        status=InstanceStatus.FROZEN,
    )


@pytest.fixture
def sessions(engine: AsyncEngine, empty_database: None) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def scheduler(
    sessions: async_sessionmaker[AsyncSession], controller: Any
) -> SchedulerRuntime:
    return SchedulerRuntime(
        session_factory=sessions,
        notifier=NullWakeUpNotifier(),
        sandbox=controller,
        settings=SchedulerSettings(
            max_recovery_attempts=3, event_retention_hours=24, batch_size=50
        ),
    )


async def a_kept_reservation(
    sessions: async_sessionmaker[AsyncSession], *, expires_in: timedelta
) -> tuple[UUID, UUID]:
    run_id = uuid4()
    async with sessions() as session:
        store = SqlSandboxStore(session)
        made = await store.reserve(
            run_id=run_id, workspace_id=uuid4(), instance=instance()
        )
        await store.keep(made.id, idle_expires_at=datetime.now(UTC) + expires_in)
        await session.commit()
        return run_id, made.instance_id


async def reservation_status(
    sessions: async_sessionmaker[AsyncSession], run_id: UUID
) -> str | None:
    async with sessions() as session:
        found = await session.execute(
            text("SELECT status FROM sandbox_reservations WHERE run_id = :run"),
            {"run": run_id},
        )
        row = found.first()
        return None if row is None else str(row.status)


async def test_a_kept_instance_past_its_deadline_is_destroyed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    run_id, sandbox_id = await a_kept_reservation(sessions, expires_in=-timedelta(seconds=1))
    controller = StandInController()

    await (await scheduler(sessions, controller)).run_once()

    assert controller.cleaned == [sandbox_id]
    assert await reservation_status(sessions, run_id) == ReservationStatus.RELEASED.value


async def test_a_kept_instance_inside_its_deadline_is_left_alone(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The whole point of keeping it: the Run is coming back."""
    run_id, _ = await a_kept_reservation(sessions, expires_in=timedelta(minutes=5))
    controller = StandInController()

    await (await scheduler(sessions, controller)).run_once()

    assert controller.cleaned == []
    assert await reservation_status(sessions, run_id) == ReservationStatus.KEPT.value


async def test_a_cleanup_that_cannot_be_confirmed_leaves_it_isolated(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Isolated rather than released, so the Run is not handed a second sandbox
    while the first may still exist. §11.5 calls this the isolated cleanup path.
    """
    run_id, _ = await a_kept_reservation(sessions, expires_in=-timedelta(seconds=1))
    controller = StandInController(fails=True)

    await (await scheduler(sessions, controller)).run_once()

    assert await reservation_status(sessions, run_id) == ReservationStatus.ISOLATED.value


async def test_an_isolated_reservation_is_retried_rather_than_forgotten(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A container the platform could not confirm gone is the one thing worth
    coming back to. Forgetting it is how a host runs out of memory overnight."""
    run_id, sandbox_id = await a_kept_reservation(sessions, expires_in=-timedelta(seconds=1))
    failing = StandInController(fails=True)
    await (await scheduler(sessions, failing)).run_once()

    recovered = StandInController()
    await (await scheduler(sessions, recovered)).run_once()

    assert recovered.cleaned == [sandbox_id]
    assert await reservation_status(sessions, run_id) == ReservationStatus.RELEASED.value


async def test_the_scan_is_idempotent(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A second pass over the same state does nothing, so a scheduler restart
    does not double-destroy or double-audit."""
    await a_kept_reservation(sessions, expires_in=-timedelta(seconds=1))
    controller = StandInController()

    await (await scheduler(sessions, controller)).run_once()
    await (await scheduler(sessions, controller)).run_once()

    assert len(controller.cleaned) == 1


async def test_a_scheduler_with_no_controller_does_not_crash(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A deployment with no tools has no Controller, and the Scheduler still has
    four other scans to run."""
    await a_kept_reservation(sessions, expires_in=-timedelta(seconds=1))

    await (await scheduler(sessions, None)).run_once()

    # And it left the reservation alone rather than releasing one it could not
    # actually reclaim.
    assert await reservation_status(sessions, uuid4()) is None
