import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.runs.application.service import LeaseLost, StateVersionConflict
from tiny_hermes.runs.domain.models import (
    CheckpointEffectStatus,
    RunCapabilities,
    RunSignal,
)
from tiny_hermes.runs.domain.slice_policy import (
    RoundOutcome,
    SliceDecision,
    decide_after_round,
)
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.model import ModelProvider, ModelRequest, ModelResponse
from tiny_hermes.runs.ports.notifier import WakeUpNotifier
from tiny_hermes.runs.ports.store import (
    ApplySignalCommand,
    ClaimedRun,
    ClaimRunCommand,
    ExecutionContext,
    RecordSliceCommand,
    RenewLeaseCommand,
)

logger = logging.getLogger(__name__)

PLATFORM = RunCapabilities(can_control=True, can_retry=True)


@dataclass(frozen=True)
class WorkerSettings:
    """How one Worker process behaves.

    ``workspace_id`` of ``None`` lets the Worker serve every workspace, which
    is what a deployed process does; tests narrow it to stay isolated.
    """

    worker_id: str
    lease_seconds: int
    max_slice_seconds: int
    idle_poll_seconds: int
    workspace_id: UUID | None = None


@dataclass
class _LeaseHandle:
    """The lease this slice currently holds, as the renewal task keeps it."""

    lease_id: UUID
    version: int
    lost: bool = False


class WorkerRuntime:
    """Claims one Head Run at a time and advances it for one execution slice.

    The Worker owns no state decision. It reports what happened in a round and
    lets ``decide_after_round`` and ``RunStateMachine`` choose the consequence.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        model: ModelProvider,
        notifier: WakeUpNotifier,
        settings: WorkerSettings,
    ) -> None:
        self._sessions = session_factory
        self._model = model
        self._notifier = notifier
        self._settings = settings
        # Renewal and slice recording both read the lease version, and both run
        # on this event loop, so one lock removes the interleaving entirely.
        self._lease_lock = asyncio.Lock()

    async def run_once(self) -> UUID | None:
        """Execute at most one slice. Returns the Run it advanced, if any."""
        claimed = await self._claim()
        if claimed is None:
            return None
        await self._execute_slice(claimed)
        return claimed.run.id

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                advanced = await self.run_once()
            except Exception:
                logger.exception("worker slice failed")
                advanced = None
            if advanced is None and not stop.is_set():
                await self._notifier.wait(self._settings.idle_poll_seconds)

    async def _claim(self) -> ClaimedRun | None:
        async with self._sessions.begin() as session:
            return await SqlRunStore(session).claim_head(
                ClaimRunCommand(
                    workspace_id=self._settings.workspace_id,
                    worker_id=self._settings.worker_id,
                    lease_seconds=self._settings.lease_seconds,
                    request_id=f"worker-{self._settings.worker_id}",
                    capabilities=PLATFORM,
                )
            )

    async def _execute_slice(self, claimed: ClaimedRun) -> None:
        workspace_id = claimed.run.workspace_id
        handle = _LeaseHandle(lease_id=claimed.lease_id, version=claimed.lease_version)
        started = monotonic()
        renewal = asyncio.create_task(
            self._renew_until_done(workspace_id, claimed.run.id, handle)
        )
        try:
            while True:
                context = await self._read_context(workspace_id, claimed.run.id)
                if context is None:
                    return
                round_started = monotonic()
                response = await self._model.complete(_request(context))
                executed_ms = int((monotonic() - round_started) * 1000)

                # Re-read after the call: a user may have asked to pause or
                # cancel while the model was working, and the request flag bumps
                # the state version this write must expect.
                after = await self._read_context(workspace_id, claimed.run.id)
                if after is None or handle.lost:
                    return
                decision = decide_after_round(
                    RoundOutcome(
                        stop_reason=response.stop_reason,
                        cancel_requested=after.cancel_requested,
                        pause_requested=after.pause_requested,
                        budget_allows=_budget_after(after, response, executed_ms),
                        slice_expired=(monotonic() - started)
                        >= self._settings.max_slice_seconds,
                    )
                )
                written = await self._record(
                    claimed, handle, after.state_version, decision, response, executed_ms
                )
                if written is False or decision.signal is not None:
                    return
        finally:
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)

    async def _read_context(
        self, workspace_id: UUID, run_id: UUID
    ) -> ExecutionContext | None:
        async with self._sessions() as session:
            return await SqlRunStore(session).execution_context(workspace_id, run_id)

    async def _record(
        self,
        claimed: ClaimedRun,
        handle: _LeaseHandle,
        state_version: int,
        decision: SliceDecision,
        response: ModelResponse,
        executed_ms: int,
    ) -> bool:
        """Persist the round. Returns False when this Worker lost the Run."""
        async with self._lease_lock:
            try:
                async with self._sessions.begin() as session:
                    store = SqlRunStore(session)
                    await store.record_slice(
                        RecordSliceCommand(
                            workspace_id=claimed.run.workspace_id,
                            run_id=claimed.run.id,
                            lease_id=handle.lease_id,
                            expected_lease_version=handle.version,
                            expected_state_version=state_version,
                            signal=decision.signal,
                            pause_reason=decision.pause_reason,
                            limit_reached=decision.limit_reached,
                            checkpoint=_checkpoint(response),
                            checkpoint_replay_safe=response.replay_safe,
                            checkpoint_effect_status=(
                                CheckpointEffectStatus.UNKNOWN
                                if response.external_effect_unknown
                                else CheckpointEffectStatus.NONE
                            ),
                            executed_ms=executed_ms,
                            model_calls=response.model_calls,
                            tokens=response.tokens,
                            request_id=f"worker-{self._settings.worker_id}",
                            capabilities=PLATFORM,
                        )
                    )
                    if decision.signal is RunSignal.SAFE_CANCEL_STARTED:
                        # Phase 2B has nothing to clean up, so the cancellation
                        # completes inside the same transaction.
                        await store.apply_signal(
                            ApplySignalCommand(
                                workspace_id=claimed.run.workspace_id,
                                run_id=claimed.run.id,
                                signal=RunSignal.SAFE_CANCEL_FINISHED,
                                request_id=f"worker-{self._settings.worker_id}",
                                capabilities=PLATFORM,
                            )
                        )
            except (LeaseLost, StateVersionConflict):
                # The Scheduler or an operator already moved this Run on.
                handle.lost = True
                return False
        return True

    async def _renew_until_done(
        self, workspace_id: UUID, run_id: UUID, handle: _LeaseHandle
    ) -> None:
        interval = max(self._settings.lease_seconds / 3, 0.05)
        while True:
            await asyncio.sleep(interval)
            async with self._lease_lock:
                if handle.lost:
                    return
                async with self._sessions.begin() as session:
                    renewed = await SqlRunStore(session).renew_lease(
                        RenewLeaseCommand(
                            workspace_id=workspace_id,
                            run_id=run_id,
                            lease_id=handle.lease_id,
                            expected_version=handle.version,
                            lease_seconds=self._settings.lease_seconds,
                        )
                    )
                if renewed is None:
                    # Someone reclaimed the Run. Abandon without writing state.
                    handle.lost = True
                    return
                handle.version = renewed.version


def _request(context: ExecutionContext) -> ModelRequest:
    """Build one round's request.

    The round number counts the Run's model calls so far rather than this
    slice's, so a scenario that needs a second round still gets one after the
    Run has been re-queued at a slice boundary.
    """
    return ModelRequest(
        policy=context.spec.model_policy,
        personality=context.spec.personality,
        input_text=context.input_text,
        round_index=context.budget.consumed_model_calls + 1,
    )


def _budget_after(
    context: ExecutionContext, response: ModelResponse, executed_ms: int
) -> bool:
    """Would the shared budget still allow another round after this one?"""
    projected = replace(
        context.budget,
        consumed_execution_ms=context.budget.consumed_execution_ms + executed_ms,
        consumed_model_calls=context.budget.consumed_model_calls + response.model_calls,
        consumed_tokens=context.budget.consumed_tokens + response.tokens,
    )
    return projected.allows_execution(datetime.now(UTC))


def _checkpoint(response: ModelResponse) -> dict[str, object]:
    return {
        "kind": "model_call",
        "stop_reason": response.stop_reason.value,
        "tokens": response.tokens,
    }
