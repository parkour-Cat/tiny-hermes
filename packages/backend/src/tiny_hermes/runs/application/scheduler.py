import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.artifacts.infrastructure.sql_store import SqlArtifactStore
from tiny_hermes.runs.domain.models import (
    PauseReason,
    RunCapabilities,
    RunSignal,
    RunState,
    WorkspaceCleanupTarget,
)
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.infrastructure.tables import RunRow
from tiny_hermes.runs.ports.notifier import WakeUpNotifier
from tiny_hermes.runs.ports.store import ApplySignalCommand
from tiny_hermes.sandbox.domain.models import InstanceStatus, ReservationStatus
from tiny_hermes.sandbox.infrastructure.sql_store import SqlSandboxStore
from tiny_hermes.session_workspace.application.cleanup import reclaim_upload
from tiny_hermes.session_workspace.application.gc import RetainedManifestOracle
from tiny_hermes.session_workspace.infrastructure.sql_store import SqlWorkspaceStore
from tiny_hermes.session_workspace.ports.objects import ObjectRef, ObjectStore

logger = logging.getLogger(__name__)

LEASES = "leases"
RECOVERY = "recovery"
HEADS = "heads"
WAITS = "waits"
COMPAT = "compat_timeouts"
RETENTION = "retention"
UPLOADS = "workspace_uploads"
ARTIFACTS = "artifact_retention"

_PLATFORM = RunCapabilities(can_control=True, can_retry=True)


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
        objects: ObjectStore | None = None,
    ) -> None:
        self._sessions = session_factory
        self._notifier = notifier
        self._settings = settings
        # Absent in a deployment with no tools, which still needs every other
        # scan this class runs.
        self._sandbox = sandbox
        # Absent likewise: workspace and artifact garbage only exists where an
        # object store does.
        self._objects = objects

    async def run_once(self) -> None:
        now = datetime.now(UTC)
        await self._reclaim_expired_leases(now)
        # Worker-side freeze/destroy failures interrupt the Run after its lease
        # is released, so they do not appear in the expired-lease scan. Hand
        # their still-live reservation to the same cleanup path explicitly.
        await self._isolate_interrupted_sandboxes()
        # A Run whose Worker died may still own a live container. Destroy that
        # container before putting the Run back in the queue; otherwise a new
        # Worker can race the cleanup and meet the abandoned reservation.
        await self._reclaim_sandboxes(now)
        await self._recover_interrupted()
        await self._repair_session_heads()
        await self._time_out_waits(now)
        await self._cancel_aged_compat_timeouts(now)
        await self._collect_expired_records(now)
        await self._collect_upload_garbage(now)
        await self._expire_artifacts(now)

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
            sandboxes = SqlSandboxStore(session)
            for run_id in doomed:
                # §11.4: 先隔离…再判断 Run 恢复. A Worker that died mid-slice
                # left a container that may still be running a command, and the
                # reservation is still `active` — which no other scan looks at.
                # Isolating it here is what hands it to `_reclaim_sandboxes`,
                # and what stops the recovered Run being given a second sandbox
                # while the first may still be alive.
                held = await sandboxes.live_for_run(run_id)
                if held is not None and held.status is not ReservationStatus.ISOLATED:
                    await sandboxes.isolate(held.id, reason="lease_expired")
                await store.reclaim_expired_lease(run_id, "scheduler-lease-expiry")
            if doomed:
                logger.info("reclaimed abandoned leases", extra={"count": len(doomed)})

    async def _isolate_interrupted_sandboxes(self) -> None:
        """Make every interrupted Run's possible container eligible for cleanup."""
        async with self._sessions.begin() as session:
            runs = SqlRunStore(session)
            sandboxes = SqlSandboxStore(session)
            for run_id in await runs.interrupted_runs(self._settings.batch_size):
                held = await sandboxes.live_for_run(run_id)
                if held is not None and held.status is not ReservationStatus.ISOLATED:
                    await sandboxes.isolate(held.id, reason="run_interrupted")

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
                    # Design §9/§6.3: a Run whose rollback recorded where it
                    # must land moves there only now, with the destruction
                    # confirmed and named.
                    await self._confirm_cleanup_intent(
                        session, reservation.run_id, reservation.instance_id
                    )
                await session.commit()

    async def _confirm_cleanup_intent(
        self, session: AsyncSession, run_id: UUID, sandbox_id: UUID
    ) -> None:
        intent = (
            await session.execute(
                select(
                    RunRow.workspace_id,
                    RunRow.workspace_cleanup_target,
                    RunRow.workspace_cleanup_sandbox_id,
                ).where(RunRow.id == run_id)
            )
        ).one_or_none()
        if intent is None or intent.workspace_cleanup_target is None:
            return
        if intent.workspace_cleanup_sandbox_id != sandbox_id:
            # The recorded intent names a different sandbox; this confirmation
            # is not the one it is waiting for.
            return
        target = WorkspaceCleanupTarget(intent.workspace_cleanup_target)
        destinations: dict[WorkspaceCleanupTarget, tuple[RunSignal, PauseReason | None]] = {
            WorkspaceCleanupTarget.PAUSED_LIMIT: (
                RunSignal.LIMIT_CLEANUP_CONFIRMED,
                PauseReason.LIMIT,
            ),
            WorkspaceCleanupTarget.FAILED_CONFLICT: (RunSignal.RECOVERY_FAILED, None),
        }
        if target not in destinations:
            # `queued` belongs to the reacquire-and-continue flow M1 does not
            # take; a row carrying it is a bug worth hearing about, not acting on.
            logger.error("unhandled cleanup target", extra={"run_id": str(run_id)})
            return
        signal, reason = destinations[target]
        try:
            await SqlRunStore(session).apply_signal(
                ApplySignalCommand(
                    workspace_id=intent.workspace_id,
                    run_id=run_id,
                    signal=signal,
                    pause_reason=reason,
                    request_id=f"scheduler-cleanup-{run_id}",
                    capabilities=_PLATFORM,
                    confirmed_sandbox_id=sandbox_id,
                )
            )
        except Exception:
            # The intent stays recorded; the next sweep tries again.
            logger.exception(
                "cleanup confirmation failed", extra={"run_id": str(run_id)}
            )

    async def _recover_interrupted(self) -> None:
        recovered: list[UUID] = []
        async with self._sessions.begin() as session:
            store = SqlRunStore(session)
            if not await store.try_scan_lock(RECOVERY):
                return
            sandboxes = SqlSandboxStore(session)
            for run_id in await store.interrupted_runs(self._settings.batch_size):
                # An isolated reservation means cleanup was not confirmed. Do
                # not queue the Run while its old container may still exist:
                # the next Worker would meet `already_reserved` and repeatedly
                # yield without making progress.
                if await sandboxes.live_for_run(run_id) is not None:
                    continue
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

    async def _cancel_aged_compat_timeouts(self, now: datetime) -> None:
        async with self._sessions.begin() as session:
            store = SqlRunStore(session)
            if not await store.try_scan_lock(COMPAT):
                return
            for run_id in await store.aged_compat_timeout_runs(
                now, self._settings.batch_size
            ):
                await store.cancel_aged_compat_timeout(
                    run_id, "scheduler-compat-timeout"
                )

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

    async def _collect_upload_garbage(self, now: datetime) -> None:
        """Task 4's cleanup planner, run against everything claimable.

        The claim happens under the scan lock; the object deletions follow the
        candidate-index order, and an uncertain reference keeps both the
        object and the claim (design §13).
        """
        if self._objects is None:
            return
        async with self._sessions.begin() as session:
            if not await SqlRunStore(session).try_scan_lock(UPLOADS):
                return
            claimed = await SqlWorkspaceStore(session).claim_cleanup(
                now, limit=self._settings.batch_size
            )
        reclaimed = 0
        for upload in claimed:
            oracle = RetainedManifestOracle(
                sessions=self._sessions,
                objects=self._objects,
                workspace_id=upload.workspace_id,
                session_id=upload.session_id,
            )
            async with self._sessions() as session:
                outcome = await reclaim_upload(
                    upload,
                    store=SqlWorkspaceStore(session),
                    objects=self._objects,
                    oracle=oracle,
                )
                await session.commit()
            if outcome.finished:
                reclaimed += 1
            else:
                logger.info(
                    "upload cleanup deferred",
                    extra={
                        "upload_id": str(upload.upload_id),
                        "reason": outcome.retry_reason,
                    },
                )
        if reclaimed:
            logger.info("reclaimed uploads", extra={"count": reclaimed})

    async def _expire_artifacts(self, now: datetime) -> None:
        """Artifact retention: rows past their expiry lose bytes, then rows.

        Object first, row second: a row without an object is a clean 404, and
        an object without a row is what the upload scan exists to find. A
        failed deletion is reported and retried, never marked done.
        """
        if self._objects is None:
            return
        async with self._sessions.begin() as session:
            if not await SqlRunStore(session).try_scan_lock(ARTIFACTS):
                return
            expired = await SqlArtifactStore(session).expired(
                now, limit=self._settings.batch_size
            )
        removed = 0
        for artifact in expired:
            try:
                await self._objects.delete_many([ObjectRef(key=artifact.object_key)])
            except Exception:
                logger.exception(
                    "artifact deletion failed", extra={"artifact_id": str(artifact.id)}
                )
                continue
            async with self._sessions.begin() as session:
                await SqlArtifactStore(session).delete(artifact.id)
            removed += 1
        if removed:
            # Identifiers and counts, never content (design §13).
            logger.info("expired artifacts removed", extra={"count": removed})

    async def _announce(self, run_id: UUID) -> None:
        async with self._sessions() as session:
            snapshot = await SqlRunStore(session).workspace_of(run_id)
        if snapshot is not None:
            await self._notifier.publish(snapshot, run_id)
