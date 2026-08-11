import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.runs.domain.models import RunState
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.notifier import WakeUpNotifier
from tiny_hermes.sandbox.domain.models import InstanceStatus
from tiny_hermes.sandbox.infrastructure.sql_store import SqlSandboxStore

logger = logging.getLogger(__name__)

LEASES = "leases"
RECOVERY = "recovery"
HEADS = "heads"
WAITS = "waits"
RETENTION = "retention"


@dataclass(frozen=True)
class SchedulerSettings:
    max_recovery_attempts: int
    event_retention_hours: int
    batch_size: int = 100


class SandboxCleanup(Protocol):
    """The Scheduler's separate authority, and only that action.

    §11.1 gives it `sandbox.cleanup` rather than the Worker's actions, so a
    Scheduler that went wrong could not execute a command in somebody's
    container — only reclaim one whose Run no longer holds a lease.
    """

    async def cleanup(self, *, run_id: UUID, sandbox_id: UUID) -> None: ...


class SchedulerRuntime:
    """Reclaims abandoned work and repairs invariants no request owns.

    Every scan is bounded, idempotent, and guarded by an advisory lock so
    several replicas cannot duplicate a side effect. The lock is an efficiency
    measure; row locks and version predicates remain the correctness argument.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        notifier: WakeUpNotifier,
        settings: SchedulerSettings,
        sandbox: SandboxCleanup | None = None,
    ) -> None:
        self._sessions = session_factory
        self._notifier = notifier
        self._settings = settings
        # Absent in a deployment with no tools, which still needs every other
        # scan this class runs.
        self._sandbox = sandbox

    async def run_once(self) -> None:
        now = datetime.now(UTC)
        await self._reclaim_expired_leases(now)
        await self._recover_interrupted()
        await self._repair_session_heads()
        await self._time_out_waits(now)
        await self._collect_expired_records(now)
        await self._reclaim_sandboxes(now)

    async def run_forever(self, stop: asyncio.Event, interval_seconds: int) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("scheduler cycle failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                continue

    async def _reclaim_expired_leases(self, now: datetime) -> None:
        async with self._sessions.begin() as session:
            store = SqlRunStore(session)
            if not await store.try_scan_lock(LEASES):
                return
            doomed = await store.expired_lease_runs(now, self._settings.batch_size)
            for run_id in doomed:
                await store.reclaim_expired_lease(run_id, "scheduler-lease-expiry")
            if doomed:
                logger.info("reclaimed abandoned leases", extra={"count": len(doomed)})

    async def _reclaim_sandboxes(self, now: datetime) -> None:
        """Destroy containers whose Run is not coming back.

        Two shapes arrive here. A `kept` reservation past its deadline is
        orderly: the instance is frozen and idle. An `isolated` one is a
        previous attempt that could not be confirmed, and it is retried rather
        than forgotten — a container the platform is unsure about is the one
        worth coming back to, and forgetting it is how a host runs out of
        memory overnight.
        """
        if self._sandbox is None:
            return
        async with self._sessions() as session:
            store = SqlSandboxStore(session)
            doomed = [
                *await store.expired_keeps(now),
                *await store.isolated(self._settings.batch_size),
            ]
            await session.commit()

        for reservation in doomed:
            async with self._sessions() as session:
                store = SqlSandboxStore(session)
                try:
                    await self._sandbox.cleanup(
                        run_id=reservation.run_id, sandbox_id=reservation.instance_id
                    )
                except Exception:
                    # Isolated rather than released, so the Run is not handed a
                    # second sandbox while the first may still exist.
                    logger.exception(
                        "sandbox cleanup failed", extra={"run_id": str(reservation.run_id)}
                    )
                    await store.isolate(reservation.id, reason="cleanup_unconfirmed")
                else:
                    await store.set_instance_status(
                        reservation.instance_id, InstanceStatus.DESTROYED
                    )
                    await store.release(reservation.id)
                await session.commit()

    async def _recover_interrupted(self) -> None:
        recovered: list[UUID] = []
        async with self._sessions.begin() as session:
            store = SqlRunStore(session)
            if not await store.try_scan_lock(RECOVERY):
                return
            for run_id in await store.interrupted_runs(self._settings.batch_size):
                state = await store.recover_interrupted(
                    run_id, self._settings.max_recovery_attempts, "scheduler-recovery"
                )
                if state is RunState.QUEUED:
                    recovered.append(run_id)
        for run_id in recovered:
            await self._announce(run_id)

    async def _repair_session_heads(self) -> None:
        async with self._sessions.begin() as session:
            store = SqlRunStore(session)
            if not await store.try_scan_lock(HEADS):
                return
            for session_id in await store.sessions_needing_repair(
                self._settings.batch_size
            ):
                await store.repair_session_head(session_id, "scheduler-head-repair")

    async def _time_out_waits(self, now: datetime) -> None:
        """Dormant until phase 3.

        Phase 2B never produces a ``waiting_external`` Run, so this scan is
        written and tested through the signal seam but has nothing to find in
        normal operation.
        """
        async with self._sessions.begin() as session:
            store = SqlRunStore(session)
            if not await store.try_scan_lock(WAITS):
                return
            for run_id in await store.expired_wait_runs(now, self._settings.batch_size):
                await store.time_out_external_wait(run_id, "scheduler-wait-timeout")

    async def _collect_expired_records(self, now: datetime) -> None:
        cutoff = now - timedelta(hours=self._settings.event_retention_hours)
        async with self._sessions.begin() as session:
            store = SqlRunStore(session)
            if not await store.try_scan_lock(RETENTION):
                return
            removed = await store.delete_expired_idempotency_records(
                now, self._settings.batch_size
            )
            pruned = await store.prune_terminal_run_events(
                cutoff, self._settings.batch_size
            )
            if removed or pruned:
                logger.info(
                    "collected expired records",
                    extra={"records": removed, "events": pruned},
                )

    async def _announce(self, run_id: UUID) -> None:
        async with self._sessions() as session:
            snapshot = await SqlRunStore(session).workspace_of(run_id)
        if snapshot is not None:
            await self._notifier.publish(snapshot, run_id)
