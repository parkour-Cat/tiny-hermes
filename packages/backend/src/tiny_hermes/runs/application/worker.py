import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.runs.application.service import LeaseLost, StateVersionConflict
from tiny_hermes.runs.domain.models import (
    Block,
    CacheStateHint,
    CanonicalMessage,
    CheckpointEffectStatus,
    RunCapabilities,
    RunEventType,
    RunSignal,
    TextBlock,
    ToolResultBlock,
)
from tiny_hermes.runs.domain.slice_policy import (
    RoundOutcome,
    SliceDecision,
    decide_after_round,
)
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.model import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
    StopReason,
    UsageQuality,
)
from tiny_hermes.runs.ports.notifier import WakeUpNotifier
from tiny_hermes.runs.ports.store import (
    AppendEventsCommand,
    ApplySignalCommand,
    ClaimedRun,
    ClaimRunCommand,
    ExecutionContext,
    RecordSliceCommand,
    RenewLeaseCommand,
    ReservedEvent,
)
from tiny_hermes.sandbox.application.controller import RefusalReason as SandboxRefusal
from tiny_hermes.sandbox.application.controller import SandboxRefused
from tiny_hermes.sandbox.domain.container_policy import DEFAULT_PROFILE
from tiny_hermes.sandbox.domain.models import CacheState
from tiny_hermes.tools.application.execute import run_tool_call
from tiny_hermes.tools.domain.registry import schemas_for

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
    #: How long a frozen instance is kept warm for this Run's next slice.
    sandbox_idle_ttl_seconds: int = 300
    workspace_id: UUID | None = None


@dataclass
class _LeaseHandle:
    """The lease this slice currently holds, as the renewal task keeps it."""

    lease_id: UUID
    version: int
    lost: bool = False


@dataclass(frozen=True)
class _Sandbox:
    """This slice's container, and what the next model call should be told."""

    sandbox_id: UUID
    hint: CacheStateHint | None


class SandboxSession(Protocol):
    """The Controller's surface, as the Worker needs it.

    A Protocol rather than the class, so the Worker holds either an in-process
    Controller or the socket client without knowing which.
    """

    async def acquire(
        self, *, run_id: UUID, lease_id: UUID, workspace_id: UUID, profile: str
    ) -> Any: ...

    async def execute(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID, command: Any
    ) -> Any: ...

    async def freeze(self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID) -> None: ...

    async def keep(self, *, run_id: UUID, sandbox_id: UUID, until: datetime) -> None: ...

    async def destroy(self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID) -> None: ...


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
        sandbox: SandboxSession | None = None,
    ) -> None:
        self._sessions = session_factory
        self._model = model
        self._notifier = notifier
        self._settings = settings
        # Optional, because a deployment with no tools configured needs none.
        # A Run that binds a tool and finds this absent fails rather than
        # running the command anywhere else — product design §16 leaves no
        # fallback, and "no sandbox configured" is not an exception to it.
        self._sandbox = sandbox
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
        box: _Sandbox | None = None
        try:
            first = await self._read_context(workspace_id, claimed.run.id)
            if first is None:
                return
            if first.spec.tools:
                box = await self._open_sandbox(claimed, handle, first)
                if box is None:
                    return

            while True:
                context = await self._read_context(workspace_id, claimed.run.id)
                if context is None:
                    return
                round_started = monotonic()
                response = await self._model.complete(_request(context, box))
                if box is not None:
                    # Only the first round of a slice is told, because only the
                    # first one is news.
                    box = replace(box, hint=None)
                executed_ms = int((monotonic() - round_started) * 1000)

                appended, results = await self._answer_tools(
                    claimed, handle, box, response, context
                )

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
                if decision.signal is not None and box is not None:
                    # Before the lease is released, per product design §16. A
                    # freeze or destroy that cannot be confirmed makes this an
                    # `interrupted` Run rather than the outcome the round had.
                    decision = await self._close_sandbox(claimed, handle, box, decision)
                    box = None if decision.signal is not RunSignal.INTERRUPTED else box

                written = await self._record(
                    claimed,
                    handle,
                    after.state_version,
                    decision,
                    response,
                    executed_ms,
                    appended=appended,
                )
                del results
                if written is False or decision.signal is not None:
                    return
        finally:
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)

    async def _open_sandbox(
        self, claimed: ClaimedRun, handle: _LeaseHandle, context: ExecutionContext
    ) -> "_Sandbox | None":
        """Acquire before the first model call of a slice, or fail the Run.

        `None` means the slice is over: either there is no Controller or it
        would not start a container, and product design §16 leaves no path that
        runs the command anywhere else.
        """
        if self._sandbox is None:
            await self._fail(claimed, handle, context, "sandbox_not_configured")
            return None
        try:
            acquired = await self._sandbox.acquire(
                run_id=claimed.run.id,
                lease_id=handle.lease_id,
                workspace_id=claimed.run.workspace_id,
                profile=DEFAULT_PROFILE.name,
            )
        except SandboxRefused as refused:
            if refused.reason is SandboxRefusal.ALREADY_RESERVED:
                # A previous slice's container is still being reclaimed. That is
                # the platform being briefly not ready, not this Run being over,
                # so the slice ends and the Run waits its turn again.
                logger.info(
                    "sandbox still held, ending the slice",
                    extra={"run_id": str(claimed.run.id)},
                )
                await self._record(
                    claimed,
                    handle,
                    context.state_version,
                    SliceDecision(RunSignal.SLICE_ENDED),
                    _no_round(),
                    executed_ms=0,
                )
                return None
            logger.exception("sandbox refused", extra={"run_id": str(claimed.run.id)})
            await self._fail(claimed, handle, context, f"sandbox_{refused.reason.value}")
            return None
        except Exception:
            logger.exception("sandbox acquire failed", extra={"run_id": str(claimed.run.id)})
            await self._fail(claimed, handle, context, "sandbox_start_failed")
            return None

        if acquired.cache_state is CacheState.RESET:
            # §11.3, and both halves of it: the event a person reads, and the
            # protected hint the model reads on the next call.
            await self._append_event(claimed, RunEventType.SANDBOX_CACHE_RESET)
            return _Sandbox(acquired.sandbox_id, hint=CacheStateHint.RESET)
        return _Sandbox(acquired.sandbox_id, hint=None)

    async def _answer_tools(
        self,
        claimed: ClaimedRun,
        handle: _LeaseHandle,
        box: "_Sandbox | None",
        response: ModelResponse,
        context: ExecutionContext,
    ) -> tuple[tuple[CanonicalMessage, ...], bool]:
        """Run whatever the round asked for, and build the turns to append."""
        if response.stop_reason is StopReason.FAILED:
            # A failed round said nothing the transcript should keep.
            return (), False
        if response.stop_reason is not StopReason.TOOL_CALL:
            return (CanonicalMessage("assistant", (TextBlock(text=response.text),)),), False

        blocks: list[Block] = []
        if response.text:
            blocks.append(TextBlock(text=response.text))
        blocks.extend(response.tool_calls)
        assistant = CanonicalMessage("assistant", tuple(blocks))

        results: list[Block] = []
        for call in response.tool_calls:
            if box is None or self._sandbox is None:
                # A tool round only follows a slice that opened a sandbox, so
                # this is unreachable today. Written as an answer rather than
                # an assertion because if it ever is reached, telling the model
                # is better than crashing a Worker mid-slice — and an assert
                # would be stripped under `-O` anyway.
                results.append(
                    ToolResultBlock(
                        call_id=call.call_id,
                        output="refused: sandbox_unavailable",
                        exit_code=126,
                        failed=True,
                    )
                )
                continue
            results.append(
                await run_tool_call(
                    controller=self._sandbox,
                    run_id=claimed.run.id,
                    lease_id=handle.lease_id,
                    sandbox_id=box.sandbox_id,
                    bound=context.spec.tools,
                    call=call,
                )
            )
        return (assistant, CanonicalMessage("tool", tuple(results))), True

    async def _close_sandbox(
        self,
        claimed: ClaimedRun,
        handle: _LeaseHandle,
        box: "_Sandbox",
        decision: SliceDecision,
    ) -> SliceDecision:
        """Freeze or destroy before the lease goes, and say so if it did not.

        A slice boundary freezes and keeps the instance warm for this Run's
        next lease. Everything else destroys it: §12.3 guarantees that a
        paused, waiting or terminal Run holds no live instance.
        """
        sandbox = self._sandbox
        if sandbox is None:
            return decision
        try:
            if decision.signal is RunSignal.SLICE_ENDED:
                await sandbox.freeze(
                    run_id=claimed.run.id,
                    lease_id=handle.lease_id,
                    sandbox_id=box.sandbox_id,
                )
                await sandbox.keep(
                    run_id=claimed.run.id,
                    sandbox_id=box.sandbox_id,
                    until=datetime.now(UTC)
                    + timedelta(seconds=self._settings.sandbox_idle_ttl_seconds),
                )
            else:
                await sandbox.destroy(
                    run_id=claimed.run.id,
                    lease_id=handle.lease_id,
                    sandbox_id=box.sandbox_id,
                )
        except Exception:
            # Not the outcome the round had. A Run that requeues after a failed
            # freeze leaves a container nobody owns and tells the console
            # everything is fine; product design §16 forbids exactly that.
            logger.exception("sandbox close failed", extra={"run_id": str(claimed.run.id)})
            return SliceDecision(RunSignal.INTERRUPTED)
        return decision

    async def _fail(
        self,
        claimed: ClaimedRun,
        handle: _LeaseHandle,
        context: ExecutionContext,
        reason: str,
    ) -> None:
        await self._record(
            claimed,
            handle,
            context.state_version,
            SliceDecision(RunSignal.FAILED),
            _no_round(failure=reason),
            executed_ms=0,
        )

    async def _append_event(self, claimed: ClaimedRun, kind: RunEventType) -> None:
        async with self._sessions.begin() as session:
            await SqlRunStore(session).append_events(
                AppendEventsCommand(
                    workspace_id=claimed.run.workspace_id,
                    run_id=claimed.run.id,
                    events=(ReservedEvent(event_type=kind),),
                )
            )

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
        appended: tuple[CanonicalMessage, ...] = (),
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
                            tokens=response.billable_tokens,
                            # A failed round said nothing the transcript should
                            # keep, so nothing is appended for it.
                            appended=appended,
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


def _no_round(failure: str | None = None) -> ModelResponse:
    """A slice that ended without a model call.

    `model_calls=0` because none was made: charging the budget for a container
    the platform could not give would let a Run run out of calls it never got.
    """
    return ModelResponse(
        stop_reason=StopReason.FAILED if failure else StopReason.CONTINUE,
        text="",
        model_calls=0,
        usage_quality=UsageQuality.UNAVAILABLE,
        failure=failure,
    )


def _request(context: ExecutionContext, box: "_Sandbox | None") -> ModelRequest:
    """Build one round's request.

    The round number counts the Run's model calls so far rather than this
    slice's, so a scenario that needs a second round still gets one after the
    Run has been re-queued at a slice boundary.
    """
    return ModelRequest(
        policy=context.spec.model_policy,
        personality=context.spec.personality,
        messages=context.messages,
        round_index=context.budget.consumed_model_calls + 1,
        tools=tuple(schemas_for(context.spec.tools)),
        cache_hint=box.hint if box is not None else None,
    )


def _budget_after(
    context: ExecutionContext, response: ModelResponse, executed_ms: int
) -> bool:
    """Would the shared budget still allow another round after this one?"""
    projected = replace(
        context.budget,
        consumed_execution_ms=context.budget.consumed_execution_ms + executed_ms,
        consumed_model_calls=context.budget.consumed_model_calls + response.model_calls,
        consumed_tokens=context.budget.consumed_tokens + response.billable_tokens,
    )
    return projected.allows_execution(datetime.now(UTC))


def _checkpoint(response: ModelResponse) -> dict[str, object]:
    """What the round was, in terms a reader of the Run can act on.

    ``usage_quality`` is recorded rather than merely implied by a zero count:
    "nothing was used" and "nobody counted" are different facts, and only one of
    them means the Token limit was meaningfully enforced.
    """
    return {
        "kind": "model_call",
        "stop_reason": response.stop_reason.value,
        "tokens": response.billable_tokens,
        "usage_quality": response.usage_quality.value,
        "failure": response.failure,
    }
