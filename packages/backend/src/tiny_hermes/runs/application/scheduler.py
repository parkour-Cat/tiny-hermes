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
from tiny_hermes.runs.domain.slice_policy import WAIT_TIMER
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
APPROVALS = "approval_expiry"
CHILDREN = "child_waits"
CASCADE = "child_cascade"
COMPAT = "compat_timeouts"
RETENTION = "retention"
UPLOADS = "workspace_uploads"
ARTIFACTS = "artifact_retention"
CHANNEL_REPLIES = "channel_replies"

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


class ChannelReplies(Protocol):
    """One pass of the channel reply queue, built per session.

    A factory rather than an object, because the dispatcher reads and writes
    rows and must do so inside the Scheduler's own transaction — handing it
    a long-lived session would put channel writes on a connection this class
    does not control.
    """

    def __call__(self, session: AsyncSession, /) -> "ReplyPass": ...


class ReplyPass(Protocol):
    async def dispatch_once(self) -> int: ...


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
        replies: ChannelReplies | None = None,
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
        # Absent where no channel can be replied through — no egress route,
        # so nothing could be sent anyway. A deployment without one still
        # receives Feishu messages and runs them; the answers settle in the
        # queue unsent rather than the Scheduler pretending to deliver them.
        self._replies = replies

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
        await self._settle_due_waits(now)
        await self._settle_child_waits()
        await self._cascade_cancel_children()
        await self._expire_approvals(now)
        await self._cancel_aged_compat_timeouts(now)
        await self._collect_expired_records(now)
        await self._collect_upload_garbage(now)
        await self._expire_artifacts(now)
        await self._dispatch_channel_replies()

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

    async def _settle_due_waits(self, now: datetime) -> None:
        """What a passed deadline means, which depends on who owned it.

        A ``timer`` is the platform's own deadline: reaching it *is* the wake,
        so the Run goes back to the queue. Every other kind waits on something
        outside — an approval, a child Run — and a deadline reached without one
        means nobody answered, which is ``paused(external_timeout)``.
        """
        woken: list[UUID] = []
        async with self._sessions.begin() as session:
            store = SqlRunStore(session)
            if not await store.try_scan_lock(WAITS):
                return
            due = await store.expired_wait_runs(now, self._settings.batch_size)
            for run_id, wait_kind in due:
                if wait_kind == WAIT_TIMER:
                    if await store.wake_external_wait(run_id, "scheduler-wait-wake"):
                        woken.append(run_id)
                else:
                    await store.time_out_external_wait(run_id, "scheduler-wait-timeout")
        for run_id in woken:
            # Announced after the transaction commits, like every other requeue
            # here: a Worker told to look before the row is visible finds a Run
            # that is still waiting and goes back to sleep.
            await self._announce(run_id)

    async def _settle_child_waits(self) -> None:
        """Hand finished children over to the parents waiting on them (§13).

        Here rather than in the child's own terminal transition, and that is the
        tenth clause rather than a convenience. A child very often finishes
        while its parent is unavailable — held by another Worker, or still
        waiting on a sibling — and a delivery attempted at that moment would
        have to either fail or block. The child writes its result where it
        cannot be lost, and this picks it up on a tick when the parent can take
        it. A parent that is busy costs a few seconds, never an answer.

        Exactly once is `result_delivered_at`, stamped inside the same
        transaction that appends the turn.
        """
        woken: list[UUID] = []
        async with self._sessions.begin() as session:
            store = SqlRunStore(session)
            if not await store.try_scan_lock(CHILDREN):
                return
            for run_id in await store.parents_awaiting_children(
                self._settings.batch_size
            ):
                if await store.settle_child_wait(run_id, "scheduler-child-settle"):
                    woken.append(run_id)
        for run_id in woken:
            # After the commit, like every other requeue here: a Worker told to
            # look before the row is visible finds a Run that is still waiting.
            await self._announce(run_id)

    async def _cascade_cancel_children(self) -> None:
        """§13's eleventh clause: no child outlives the parent that wanted it.

        A sweep rather than something hung off the cancel path, because a
        parent reaches a terminal state down several routes — a person
        cancelling it, a failure, a deadline — and a cascade attached to one of
        them is a cascade the other routes do not get. A child still running
        for a parent that has gone is spending the root budget on work nobody
        will read.
        """
        async with self._sessions.begin() as session:
            store = SqlRunStore(session)
            if not await store.try_scan_lock(CASCADE):
                return
            for run_id in await store.cancelled_parents_with_children(
                self._settings.batch_size
            ):
                await store.cascade_cancel_children(run_id, "scheduler-child-cascade")

    async def _expire_approvals(self, now: datetime) -> None:
        """§16.3's deadline, enforced by the only process that can.

        A Run in `waiting_approval` holds no lease and no container, so nothing
        else is watching its clock. Without this sweep a question nobody
        answered would keep a Session's head forever.

        Not announced afterwards, unlike a timer that came due: an expired
        approval pauses the Run rather than requeueing it, and there is nothing
        for a Worker to pick up.
        """
        async with self._sessions.begin() as session:
            store = SqlRunStore(session)
            if not await store.try_scan_lock(APPROVALS):
                return
            for approval_id, run_id in await store.expired_approvals(
                now, self._settings.batch_size
            ):
                await store.expire_approval(
                    approval_id, run_id, "scheduler-approval-expiry", now
                )

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

    async def _dispatch_channel_replies(self) -> None:
        """§19.3's fourth job: the result goes back to whoever asked for it.

        Here rather than in the Worker because a Worker that finished a Run
        and died before telling anybody would lose the reply exactly when it
        mattered, with nothing left to say so. The row in `channel_events`
        outlives the process, and this scan finding it again is a retry
        nobody had to write.

        Under the same scan lock as every other scan: two Scheduler replicas
        would otherwise both read the queue, and Feishu would deliver the
        answer twice.
        """
        if self._replies is None:
            return
        async with self._sessions.begin() as session:
            if not await SqlRunStore(session).try_scan_lock(CHANNEL_REPLIES):
                return
            sent = await self._replies(session).dispatch_once()
            if sent:
                logger.info("channel replies delivered", extra={"replies": sent})

    async def _announce(self, run_id: UUID) -> None:
        async with self._sessions() as session:
            snapshot = await SqlRunStore(session).workspace_of(run_id)
        if snapshot is not None:
            await self._notifier.publish(snapshot, run_id)
