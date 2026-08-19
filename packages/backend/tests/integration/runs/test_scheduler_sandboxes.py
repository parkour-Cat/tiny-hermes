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
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tiny_hermes.runs.application.scheduler import SchedulerRuntime, SchedulerSettings
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.domain.models import RunCapabilities
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.store import ClaimRunCommand
from tiny_hermes.sandbox.domain.models import (
    CacheState,
    InstanceStatus,
    ReservationStatus,
    SandboxInstance,
)
from tiny_hermes.sandbox.infrastructure.sql_store import SqlSandboxStore
from tiny_hermes.sandbox.transport.adapter import AcquiredSandbox

from ..conftest import VALID_SPEC

DIGEST = "sha256:" + "f" * 64


@dataclass
class StandInController:
    """The Controller's cleanup surface, and whether it could confirm."""

    fails: bool = False
    order: list[str] | None = None
    cleaned: list[UUID] = field(default_factory=list[UUID])

    async def cleanup(self, *, run_id: UUID, sandbox_id: UUID) -> None:
        del run_id
        if self.fails:
            raise RuntimeError("the daemon did not answer")
        self.cleaned.append(sandbox_id)
        if self.order is not None:
            self.order.append("sandbox_destroyed")


@dataclass
class ReleaseThenFailCleanup:
    """A concurrent cleanup wins after the Scheduler selected stale work."""

    sessions: async_sessionmaker[AsyncSession]

    async def cleanup(self, *, run_id: UUID, sandbox_id: UUID) -> None:
        del sandbox_id
        async with self.sessions.begin() as session:
            store = SqlSandboxStore(session)
            reservation = await store.live_for_run(run_id)
            assert reservation is not None
            await store.release(reservation.id)
        raise RuntimeError("the stale Scheduler cleanup was refused")


@dataclass
class RecordingNotifier:
    order: list[str]

    async def publish(self, workspace_id: UUID, run_id: UUID) -> None:
        del workspace_id, run_id
        self.order.append("run_requeued")

    async def wait(self, timeout_seconds: float) -> bool:
        del timeout_seconds
        return False

    async def close(self) -> None:
        return None


class PersistingDestroyFailure:
    """The Worker's Controller seam with a real active reservation behind it."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._instance = instance()

    async def acquire(
        self,
        *,
        run_id: UUID,
        lease_id: UUID,
        workspace_id: UUID,
        profile: str,
        session_id: UUID | None = None,
    ) -> AcquiredSandbox:
        del lease_id, profile, session_id
        async with self._sessions() as session:
            await SqlSandboxStore(session).reserve(
                run_id=run_id, workspace_id=workspace_id, instance=self._instance
            )
            await session.commit()
        return AcquiredSandbox(self._instance.id, CacheState.RESET)

    async def execute(self, **_: Any) -> None:
        raise AssertionError("the complete model scenario does not call a tool")

    async def freeze(self, **_: Any) -> None:
        raise AssertionError("the completed Run destroys rather than freezes")

    async def thaw(self, **_: Any) -> None:
        raise AssertionError("the completed Run has nothing to thaw")

    async def keep(self, **_: Any) -> None:
        raise AssertionError("the completed Run is not kept warm")

    async def destroy(self, **_: Any) -> None:
        raise RuntimeError("the daemon did not confirm destroy")

    async def cleanup(self, **_: Any) -> None:
        raise AssertionError("a completed Run still holds a lease, so it destroys")


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
    sessions: async_sessionmaker[AsyncSession], controller: Any, notifier: Any | None = None
) -> SchedulerRuntime:
    return SchedulerRuntime(
        session_factory=sessions,
        notifier=notifier or NullWakeUpNotifier(),
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


async def test_a_stale_cleanup_failure_cannot_reisolate_a_released_reservation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    run_id, _ = await a_kept_reservation(
        sessions, expires_in=-timedelta(seconds=1)
    )

    await (await scheduler(sessions, ReleaseThenFailCleanup(sessions))).run_once()

    assert await reservation_status(sessions, run_id) == ReservationStatus.RELEASED.value


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


async def test_an_expired_lease_isolates_the_sandbox_it_left_behind(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    published_agent: str,
) -> None:
    """§11.4: 先隔离…再判断 Run 恢复.

    A Worker that died mid-slice leaves an `active` reservation, which neither
    the keep scan nor the isolated scan looks at — so without this the container
    is never reclaimed at all. The restart drill found exactly that: a container
    outliving its Run while every unit test passed, because the two scans were
    written without the step that connects them.

    Driven through a real claim and a real expired lease, because the bug was
    precisely that the paths were tested apart.
    """
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    session_id = client.post(
        "/api/v1/sessions", headers=scope, json={"agent_id": published_agent}
    ).json()["id"]
    run_id = UUID(
        client.post(
            "/api/v1/runs",
            headers={**scope, "Idempotency-Key": uuid4().hex},
            json={"session_id": session_id, "input": "hold a sandbox"},
        ).json()["id"]
    )

    async with sessions.begin() as session:
        claimed = await SqlRunStore(session).claim_head(
            ClaimRunCommand(
                workspace_id=None,
                worker_id="doomed-worker",
                lease_seconds=30,
                request_id="claim-doomed",
                capabilities=RunCapabilities(can_control=True, can_retry=True),
            )
        )
    assert claimed is not None

    async with sessions() as session:
        await SqlSandboxStore(session).reserve(
            run_id=run_id, workspace_id=claimed.run.workspace_id, instance=instance()
        )
        await session.commit()

    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE worker_leases SET expires_at = now() - interval '1 minute' "
                "WHERE run_id = :id"
            ),
            {"id": run_id},
        )

    order: list[str] = []
    controller = StandInController(order=order)
    notifier = RecordingNotifier(order)
    await (await scheduler(sessions, controller, notifier)).run_once()

    # Isolated by the lease scan and destroyed by the sandbox scan, in that
    # order and in one cycle.
    assert len(controller.cleaned) == 1
    assert await reservation_status(sessions, run_id) == ReservationStatus.RELEASED.value
    assert order == ["sandbox_destroyed", "run_requeued"]


async def test_an_interrupted_run_waits_until_its_sandbox_cleanup_succeeds(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    published_agent: str,
) -> None:
    """A possible live container blocks recovery instead of causing a reserve loop."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    session_id = client.post(
        "/api/v1/sessions", headers=scope, json={"agent_id": published_agent}
    ).json()["id"]
    run_id = UUID(
        client.post(
            "/api/v1/runs",
            headers={**scope, "Idempotency-Key": uuid4().hex},
            json={"session_id": session_id, "input": "hold a sandbox"},
        ).json()["id"]
    )

    async with sessions.begin() as session:
        claimed = await SqlRunStore(session).claim_head(
            ClaimRunCommand(
                workspace_id=None,
                worker_id="doomed-worker",
                lease_seconds=30,
                request_id="claim-doomed",
                capabilities=RunCapabilities(can_control=True, can_retry=True),
            )
        )
    assert claimed is not None

    async with sessions() as session:
        await SqlSandboxStore(session).reserve(
            run_id=run_id, workspace_id=claimed.run.workspace_id, instance=instance()
        )
        await session.commit()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE worker_leases SET expires_at = now() - interval '1 minute' "
                "WHERE run_id = :id"
            ),
            {"id": run_id},
        )

    order: list[str] = []
    controller = StandInController(fails=True, order=order)
    runtime = await scheduler(sessions, controller, RecordingNotifier(order))

    await runtime.run_once()

    async with engine.connect() as connection:
        status, recovery_attempts = (
            await connection.execute(
                text(
                    "SELECT status, recovery_attempts FROM runs WHERE id = :id"
                ),
                {"id": run_id},
            )
        ).one()
    assert status == "interrupted"
    assert recovery_attempts == 0
    assert await reservation_status(sessions, run_id) == ReservationStatus.ISOLATED.value
    assert order == []

    controller.fails = False
    await runtime.run_once()

    async with engine.connect() as connection:
        status = await connection.scalar(
            text("SELECT status FROM runs WHERE id = :id"), {"id": run_id}
        )
    assert status == "queued"
    assert order == ["sandbox_destroyed", "run_requeued"]


async def test_worker_close_failure_is_cleaned_before_the_run_recovers(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
) -> None:
    """A failed Worker destroy leaves `active`, not an expired Worker lease."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    alias = f"close-failure-{uuid4().hex[:8]}"
    agent = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Close failure", "alias": alias}
    ).json()
    draft = client.put(
        f"/api/v1/agents/{agent['id']}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": {**VALID_SPEC, "tools": ["shell.exec"]}},
    ).json()
    published = client.post(
        f"/api/v1/agents/{agent['id']}/publish",
        headers=scope,
        json={"expected_revision": draft["revision"]},
    )
    assert published.status_code == 201
    session_id = client.post(
        "/api/v1/sessions", headers=scope, json={"agent_id": agent["id"]}
    ).json()["id"]
    run_id = UUID(
        client.post(
            "/api/v1/runs",
            headers={**scope, "Idempotency-Key": uuid4().hex},
            json={"session_id": session_id, "input": "finish then close"},
        ).json()["id"]
    )

    await WorkerRuntime(
        session_factory=sessions,
        model=DeterministicModelProvider(delay_ms=0),
        notifier=NullWakeUpNotifier(),
        sandbox=PersistingDestroyFailure(sessions),
        settings=WorkerSettings(
            worker_id="worker-close-failure",
            lease_seconds=30,
            max_slice_seconds=30,
            idle_poll_seconds=1,
        ),
    ).run_once()

    async with engine.connect() as connection:
        status = await connection.scalar(
            text("SELECT status FROM runs WHERE id = :id"), {"id": run_id}
        )
    assert status == "interrupted"
    assert await reservation_status(sessions, run_id) == ReservationStatus.ACTIVE.value

    order: list[str] = []
    await (
        await scheduler(
            sessions, StandInController(order=order), RecordingNotifier(order)
        )
    ).run_once()

    async with engine.connect() as connection:
        status = await connection.scalar(
            text("SELECT status FROM runs WHERE id = :id"), {"id": run_id}
        )
    assert status == "queued"
    assert await reservation_status(sessions, run_id) == ReservationStatus.RELEASED.value
    assert order == ["sandbox_destroyed", "run_requeued"]
