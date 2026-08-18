import asyncio
import inspect
import logging
import shlex
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.agents.domain.models import ContextBudget
from tiny_hermes.artifacts.application.service import ArtifactLimits, ArtifactRecorder
from tiny_hermes.runs.application.service import LeaseLost, StateVersionConflict
from tiny_hermes.runs.application.tool_answers import (
    answer_platform_tool,
    answer_skill_load,
    answer_skill_propose,
)
from tiny_hermes.runs.domain.context_budget import (
    ContextPlan,
    SkillSummary,
    plan_context,
)
from tiny_hermes.runs.domain.goal import (
    CompletionCheck,
    GoalEvidence,
    GoalProposal,
    GoalVerdict,
    judge,
)
from tiny_hermes.runs.domain.models import (
    SAFETY_PREAMBLE,
    Block,
    CacheStateHint,
    CanonicalMessage,
    CheckpointEffectStatus,
    PauseReason,
    RunCapabilities,
    RunEventType,
    RunSignal,
    TextBlock,
    ToolResultBlock,
    WorkspaceCleanupTarget,
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
from tiny_hermes.runs.ports.proposals import SkillProposals
from tiny_hermes.runs.ports.skills import SkillLibrary
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
from tiny_hermes.sandbox.domain.command import SandboxCommand
from tiny_hermes.sandbox.domain.container_policy import DEFAULT_PROFILE
from tiny_hermes.sandbox.domain.models import CacheState
from tiny_hermes.session_workspace.application.committer import SqlWorkspaceLedger
from tiny_hermes.session_workspace.application.service import (
    SessionWorkspaceService,
    WorkspaceCheckpoint,
    WorkspaceIntegrityFailed,
    WorkspaceRestore,
)
from tiny_hermes.session_workspace.domain.models import (
    CheckpointStatus,
    UnsupportedWorkspaceEntry,
    WorkspaceQuota,
)
from tiny_hermes.session_workspace.infrastructure.sandbox_port import (
    ControllerWorkspacePort,
    WorkspaceGateway,
)
from tiny_hermes.session_workspace.ports.objects import ObjectStore
from tiny_hermes.tools.application.execute import (
    ArtifactUpload,
    StreamedCommandRunner,
    run_tool_call,
)
from tiny_hermes.tools.domain.files import DATA_ROOT, FILE_HELPER, changes_workspace
from tiny_hermes.tools.domain.registry import (
    DEFAULT_OUTPUT_BYTES,
    PLATFORM_TOOLS,
    schemas_for,
)

logger = logging.getLogger(__name__)

PLATFORM = RunCapabilities(can_control=True, can_retry=True)

#: How long a declared verification command may take. Long enough for a real
#: test suite, short enough that a check which hangs pauses the Run for a
#: person inside one slice rather than spending the whole budget on silence.
_VERIFICATION_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class WorkspaceRuntime:
    """Everything the Worker needs to give Sessions persistent files.

    Optional as a whole: a deployment without object storage runs exactly the
    3B behavior, and a test that is not about workspaces never meets one.
    """

    objects: ObjectStore
    quota: WorkspaceQuota
    staging_ttl_seconds: int
    export_limit: int
    artifact_max_bytes: int = 104_857_600
    run_artifact_max_bytes: int = 524_288_000
    #: Seven days: Artifacts are Run output, not workspace state, and the
    #: Scheduler already expires rows by ``expires_at``. A workspace policy
    #: field can replace this default when 4B surfaces retention in the UI.
    artifact_retention_seconds: int = 604_800
    preview_bytes: int = DEFAULT_OUTPUT_BYTES


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
class _RoundWork:
    """What one round's tool calls produced.

    ``wait_seconds`` is set only when the round called `platform.wait` and the
    call was well formed. It is a *request*: `judge` decides whether waiting is
    what this round actually gets, the same as it does for a completion claim.
    """

    appended: tuple[CanonicalMessage, ...]
    wrote: bool
    wait_seconds: int | None = None
    #: Facts the round's own tool calls produced, written in the transaction
    #: that records the round. A `skill_loaded` written separately could
    #: survive a rolled-back round, and the next round would then believe it
    #: was holding text that is not in its conversation.
    events: tuple[ReservedEvent, ...] = ()


@dataclass(frozen=True)
class _Judged:
    """One round's number and what the platform decided about it.

    The two travel together because neither is legible alone: a `continue`
    without a round number does not say how long this has been going on, and a
    round number without a verdict does not say why it went on.
    """

    round: int
    verdict: GoalVerdict


@dataclass(frozen=True)
class _Sandbox:
    """This slice's container, and what the next model call should be told."""

    sandbox_id: UUID
    hint: CacheStateHint | None
    #: The committed revision the workspace currently equals — the base every
    #: checkpoint this slice makes must name (design §8).
    revision: UUID | None = None


class SandboxSession(Protocol):
    """The Controller's surface, as the Worker needs it.

    A Protocol rather than the class, so the Worker holds either an in-process
    Controller or the socket client without knowing which.
    """

    async def acquire(
        self,
        *,
        run_id: UUID,
        lease_id: UUID,
        workspace_id: UUID,
        profile: str,
        session_id: UUID | None = None,
    ) -> Any: ...

    async def execute(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID, command: Any
    ) -> Any: ...

    async def freeze(self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID) -> None: ...

    async def thaw(self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID) -> None: ...

    async def keep(self, *, run_id: UUID, sandbox_id: UUID, until: datetime) -> None: ...

    async def destroy(self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID) -> None: ...

    async def cleanup(self, *, run_id: UUID, sandbox_id: UUID) -> None: ...


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
        workspace: WorkspaceRuntime | None = None,
        skills: SkillLibrary | None = None,
        proposals: SkillProposals | None = None,
    ) -> None:
        self._sessions = session_factory
        self._model = model
        self._notifier = notifier
        self._settings = settings
        # Optional for the same reason the sandbox is: a deployment with no
        # skill catalog needs none. An Agent that bound a skill and finds this
        # absent is told so in the tool result rather than reading nothing and
        # believing the skill was empty.
        self._skills = skills
        self._proposals = proposals
        # Optional, because a deployment with no tools configured needs none.
        # A Run that binds a tool and finds this absent fails rather than
        # running the command anywhere else — product design §16 leaves no
        # fallback, and "no sandbox configured" is not an exception to it.
        self._sandbox = sandbox
        self._workspace = workspace
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
            if any(tool not in PLATFORM_TOOLS for tool in first.spec.tools):
                # Platform tools do not run anywhere. An Agent that binds only
                # those has nothing to put in a container, and starting one for
                # it would mean a Run about to wait held an instance while it
                # waited — the opposite of what §12.3 promises.
                box = await self._open_sandbox(claimed, handle, first)
                if box is None:
                    return

            while True:
                context = await self._read_context(workspace_id, claimed.run.id)
                if context is None:
                    return
                plan = _plan(context)
                if not plan.fits:
                    await self._overflow(claimed, handle, box, context, plan)
                    return
                if plan.changed:
                    await self._record_planning(claimed, plan)
                round_started = monotonic()
                response = await self._model.complete(_request(context, box, plan))
                if box is not None:
                    # Only the first round of a slice is told, because only the
                    # first one is news.
                    box = replace(box, hint=None)
                executed_ms = int((monotonic() - round_started) * 1000)

                work = await self._answer_tools(
                    claimed, handle, box, response, context
                )
                appended, wrote = work.appended, work.wrote
                # Before the re-read, for the same reason the tool calls are:
                # a cancellation that arrives while a check is running should
                # be seen by the read that follows it.
                evidence = await self._completion_evidence(
                    claimed, handle, box, context, response
                )

                # Re-read after the call: a user may have asked to pause or
                # cancel while the model was working, and the request flag bumps
                # the state version this write must expect.
                after = await self._read_context(workspace_id, claimed.run.id)
                if after is None or handle.lost:
                    return
                now = datetime.now(UTC)
                compat_expired = (
                    after.compat_deadline_at is not None
                    and now >= after.compat_deadline_at
                )
                verdict = judge(
                    GoalProposal(
                        stop_reason=response.stop_reason,
                        wait_seconds=work.wait_seconds,
                    ),
                    evidence,
                )
                # The number the model was given for this round, not the one
                # the next read would compute: the two differ the moment this
                # round's own model call is counted.
                judged = _Judged(round=_round_index(context), verdict=verdict)
                if verdict.instruction is not None:
                    # §12.1: continue 生成下一轮指令. Recorded even if the Run
                    # then pauses for some other reason — why the platform
                    # disagreed with the claim stays true, and the next round
                    # after a resume is the one that needs to read it.
                    appended = (
                        *appended,
                        CanonicalMessage(
                            role="user",
                            blocks=(TextBlock(text=verdict.instruction),),
                            author="platform",
                        ),
                    )
                decision = decide_after_round(
                    RoundOutcome(
                        verdict=verdict,
                        cancel_requested=after.cancel_requested,
                        pause_requested=after.pause_requested,
                        budget_allows=_budget_after(after, response, executed_ms),
                        slice_expired=(monotonic() - started)
                        >= self._settings.max_slice_seconds,
                        hold_slice=(
                            after.compat_deadline_at is not None and not compat_expired
                        ),
                        compat_window_expired=compat_expired,
                    )
                )

                if wrote and box is not None and self._workspace is not None:
                    # Design §8: after every tool round that may have changed
                    # the data mount, one frozen scan and one commit cover the
                    # round's effects before the next model call.
                    continuation = await self._checkpoint_round(
                        claimed, handle, box, after, decision, response, executed_ms,
                        appended, judged, work.events,
                    )
                    if continuation is None:
                        return
                    box = continuation
                    continue

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
                    events=work.events,
                    judged=judged,
                )
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
                session_id=claimed.run.session_id,
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
            box = _Sandbox(acquired.sandbox_id, hint=CacheStateHint.RESET)
        else:
            box = _Sandbox(acquired.sandbox_id, hint=None)

        if self._workspace is None:
            return box
        if any(name.startswith("file.") for name in context.spec.tools):
            if not await self._file_safety_holds(claimed, handle, box):
                await self._discard_sandbox(claimed, handle, box)
                await self._fail(claimed, handle, context, "file_safety_unavailable")
                return None
        if acquired.cache_state is not CacheState.RESET:
            # A warm instance still holds the state this Run itself committed;
            # only the base pointer needs reading.
            record = await SqlWorkspaceLedger(self._sessions).current_revision(
                claimed.run.workspace_id, claimed.run.session_id
            )
            return replace(box, revision=None if record is None else record.revision_id)
        return await self._restore_workspace(claimed, handle, box, context)

    async def _restore_workspace(
        self,
        claimed: ClaimedRun,
        handle: _LeaseHandle,
        box: _Sandbox,
        context: ExecutionContext,
    ) -> "_Sandbox | None":
        """Design §7: freeze, verify, stream, re-scan, and only then a model."""
        sandbox = self._sandbox
        if sandbox is None:  # pragma: no cover - guarded by the caller
            return None
        current = await SqlWorkspaceLedger(self._sessions).current_revision(
            claimed.run.workspace_id, claimed.run.session_id
        )
        if current is None:
            # An empty workspace needs no import, so the fresh container is
            # not frozen for nothing.
            return box
        service = self._workspace_service(claimed, handle, box)
        try:
            await sandbox.freeze(
                run_id=claimed.run.id, lease_id=handle.lease_id, sandbox_id=box.sandbox_id
            )
            result = await service.restore(
                WorkspaceRestore(
                    workspace_id=claimed.run.workspace_id,
                    session_id=claimed.run.session_id,
                    run_id=claimed.run.id,
                )
            )
            await sandbox.thaw(
                run_id=claimed.run.id, lease_id=handle.lease_id, sandbox_id=box.sandbox_id
            )
        except WorkspaceIntegrityFailed as broken:
            # Verified-bad stored state: an operator must repair it. Retrying
            # into a lucky success is exactly what must not happen (design §7).
            logger.error(
                "workspace integrity failed", extra={"run_id": str(claimed.run.id)}
            )
            await self._append_event(
                claimed,
                RunEventType.WORKSPACE_INTEGRITY_FAILED,
                payload={"detail": str(broken)[:200]},
            )
            await self._discard_sandbox(claimed, handle, box)
            await self._fail(claimed, handle, context, "workspace_integrity_failed")
            return None
        except Exception:
            # Transient storage or transport trouble: interrupted, for bounded
            # recovery. The partially restored sandbox is destroyed — the
            # model never sees a half-written tree.
            logger.exception(
                "workspace restore unavailable", extra={"run_id": str(claimed.run.id)}
            )
            await self._append_event(claimed, RunEventType.WORKSPACE_STORAGE_UNAVAILABLE)
            await self._discard_sandbox(claimed, handle, box)
            await self._record(
                claimed,
                handle,
                context.state_version,
                SliceDecision(RunSignal.INTERRUPTED),
                _no_round(),
                executed_ms=0,
            )
            return None
        return replace(box, revision=result.revision_id)

    async def _completion_evidence(
        self,
        claimed: ClaimedRun,
        handle: _LeaseHandle,
        box: "_Sandbox | None",
        context: ExecutionContext,
        response: ModelResponse,
    ) -> GoalEvidence:
        """Check the claim, in this Run's own sandbox.

        Only a claim is worth checking: a round that asked for a tool has not
        said it is finished, and an Agent that declared no condition has given
        the platform nothing to check. Both of those leave here having run no
        command at all, which is why an Agent published before this slice
        costs exactly what it used to.

        Whatever the checks write is not committed. A check observes; the
        Agent's own rounds are what produce the workspace, and a passing check
        that quietly added files to the record would make the record wrong.
        """
        condition = context.spec.completion
        if condition is None or response.stop_reason is not StopReason.COMPLETED:
            return GoalEvidence(declared=condition is not None)
        if box is None or self._sandbox is None:  # pragma: no cover - publish refuses it
            return GoalEvidence(declared=True, observable=False)

        checks: list[CompletionCheck] = []
        for path in condition.expected_artifacts:
            # `test -e` and not a stat of the host: the artifact is a path
            # inside the sandbox, and this is the only place it exists.
            met = await self._check_holds(
                claimed, handle, box, f"test -e {shlex.quote(f'{DATA_ROOT}/{path}')}", 30
            )
            if met is None:
                return GoalEvidence(declared=True, observable=False)
            checks.append(CompletionCheck(name=path, met=met))

        if condition.verification_command is not None:
            met = await self._check_holds(
                claimed,
                handle,
                box,
                condition.verification_command,
                _VERIFICATION_TIMEOUT_SECONDS,
            )
            if met is None:
                return GoalEvidence(declared=True, observable=False)
            checks.append(CompletionCheck(name=condition.verification_command, met=met))

        return GoalEvidence(declared=True, checks=tuple(checks))

    async def _check_holds(
        self,
        claimed: ClaimedRun,
        handle: _LeaseHandle,
        box: _Sandbox,
        line: str,
        timeout_seconds: int,
    ) -> bool | None:
        """Run one check. ``None`` means it did not answer.

        A command that was killed on its timeout, or a controller that refused
        to run it, said nothing about whether the goal was met. Reporting
        either as "not met" would loop a healthy Run against a broken sandbox
        until the budget stopped it, and reporting it as met would accept a
        claim on no evidence.
        """
        sandbox = self._sandbox
        if sandbox is None:  # pragma: no cover - guarded by the caller
            return None
        try:
            result = await sandbox.execute(
                run_id=claimed.run.id,
                lease_id=handle.lease_id,
                sandbox_id=box.sandbox_id,
                command=SandboxCommand(
                    # The same shell the Agent's own commands get, so the
                    # verification an author wrote and tested by hand is the
                    # one that runs — and so §16's no-host-fallback rule
                    # covers it without a second execution path existing.
                    argv=["/bin/bash", "-lc", line],
                    cwd=DATA_ROOT,
                    timeout_seconds=timeout_seconds,
                    output_limit=DEFAULT_OUTPUT_BYTES,
                ),
            )
        except Exception:
            logger.exception(
                "completion check could not run", extra={"run_id": str(claimed.run.id)}
            )
            return None
        if result.timed_out:
            return None
        return int(result.exit_code) == 0

    async def _file_safety_holds(
        self, claimed: ClaimedRun, handle: _LeaseHandle, box: _Sandbox
    ) -> bool:
        """The openat2 probe: file tools exist only where the kernel can hold
        the design's promises (design §5.2)."""
        sandbox = self._sandbox
        if sandbox is None:  # pragma: no cover - guarded by the caller
            return False
        try:
            probe = await sandbox.execute(
                run_id=claimed.run.id,
                lease_id=handle.lease_id,
                sandbox_id=box.sandbox_id,
                command=SandboxCommand(
                    argv=[FILE_HELPER, "--root", DATA_ROOT, "probe"],
                    cwd=DATA_ROOT,
                    timeout_seconds=10,
                    output_limit=1024,
                ),
            )
        except Exception:
            logger.exception("file safety probe failed", extra={"run_id": str(claimed.run.id)})
            return False
        return int(probe.exit_code) == 0

    async def _discard_sandbox(
        self, claimed: ClaimedRun, handle: _LeaseHandle, box: _Sandbox
    ) -> None:
        """Best-effort teardown on a path that is already failing."""
        sandbox = self._sandbox
        if sandbox is None:
            return
        try:
            await sandbox.destroy(
                run_id=claimed.run.id, lease_id=handle.lease_id, sandbox_id=box.sandbox_id
            )
        except Exception:
            logger.exception("sandbox discard failed", extra={"run_id": str(claimed.run.id)})

    def _workspace_service(
        self, claimed: ClaimedRun, handle: _LeaseHandle, box: _Sandbox
    ) -> SessionWorkspaceService:
        runtime = self._workspace
        sandbox = self._sandbox
        if runtime is None or sandbox is None:  # pragma: no cover - callers check
            raise RuntimeError("workspace runtime is not configured")
        # A workspace-configured deployment wires a gateway-capable sandbox
        # (the socket adapter carries all three workspace calls); the protocol
        # stays narrow so 3B-only fakes and deployments never learn of them.
        port = ControllerWorkspacePort(
            gateway=cast(WorkspaceGateway, sandbox),
            run_id=claimed.run.id,
            lease_id=handle.lease_id,
            sandbox_id=box.sandbox_id,
            export_limit=runtime.export_limit,
        )
        return SessionWorkspaceService(
            ledger=SqlWorkspaceLedger(self._sessions),
            objects=runtime.objects,
            sandbox=port,
            staging_ttl_seconds=runtime.staging_ttl_seconds,
        )

    def _open_artifact(self, claimed: ClaimedRun) -> Callable[[], ArtifactUpload] | None:
        """A factory so the recorder is constructed only after the preview overflows."""
        runtime = self._workspace
        if runtime is None:
            return None

        def open_recorder() -> ArtifactRecorder:
            return ArtifactRecorder(
                sessions=self._sessions,
                objects=runtime.objects,
                workspace_id=claimed.run.workspace_id,
                session_id=claimed.run.session_id,
                run_id=claimed.run.id,
                filename="command-output.log",
                media_type="text/plain",
                limits=ArtifactLimits(
                    artifact_max_bytes=runtime.artifact_max_bytes,
                    run_artifact_max_bytes=runtime.run_artifact_max_bytes,
                    retention_seconds=runtime.artifact_retention_seconds,
                    staging_ttl_seconds=runtime.staging_ttl_seconds,
                ),
            )

        return open_recorder

    async def _answer_tools(
        self,
        claimed: ClaimedRun,
        handle: _LeaseHandle,
        box: "_Sandbox | None",
        response: ModelResponse,
        context: ExecutionContext,
    ) -> "_RoundWork":
        """Run whatever the round asked for, and build the turns to append.

        ``wrote`` answers design §8's question: may this round have changed
        `/workspace/data`? A refused call never ran, so it cannot have; a
        successful `file.read` looked and touched nothing.
        """
        if response.stop_reason is StopReason.FAILED:
            # A failed round said nothing the transcript should keep.
            return _RoundWork((), False)
        if response.stop_reason is not StopReason.TOOL_CALL:
            return _RoundWork(
                (CanonicalMessage("assistant", (TextBlock(text=response.text),)),), False
            )

        blocks: list[Block] = []
        if response.text:
            blocks.append(TextBlock(text=response.text))
        blocks.extend(response.tool_calls)
        assistant = CanonicalMessage("assistant", tuple(blocks))

        wrote = False
        wait_seconds: int | None = None
        results: list[Block] = []
        events: list[ReservedEvent] = []
        # Counted across the whole Run and carried forward inside this round:
        # eight loads is a Run's ceiling, not a slice's, and a round asking
        # three times must not get three answers out of one round's worth of
        # room.
        loaded = list(context.loaded_skills)
        for call in response.tool_calls:
            if call.name == "skill.load":
                answered, event = await answer_skill_load(self._skills, context, call, loaded)
                results.append(answered)
                if event is not None:
                    events.append(event)
                    loaded.append(UUID(str(event.payload["skill_version_id"])))
                continue
            if call.name == "skill.propose":
                answered, event = await answer_skill_propose(
                    self._proposals, context, call
                )
                results.append(answered)
                if event is not None:
                    events.append(event)
                continue
            if call.name in PLATFORM_TOOLS:
                # Answered here, never sent down. What this asks for happens to
                # the Run; the Controller has nothing to do with it.
                answered, seconds = answer_platform_tool(call, context.spec.tools)
                results.append(answered)
                if seconds is not None:
                    # Last one wins, and one is all a round can act on: the Run
                    # enters a single `waiting_external` with a single deadline.
                    wait_seconds = seconds
                continue
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
            answer = await run_tool_call(
                controller=self._sandbox,
                run_id=claimed.run.id,
                lease_id=handle.lease_id,
                sandbox_id=box.sandbox_id,
                bound=context.spec.tools,
                call=call,
                streamer=_streamer_of(self._sandbox),
                open_artifact=self._open_artifact(claimed),
                preview_limit=(
                    self._workspace.preview_bytes
                    if self._workspace is not None
                    else DEFAULT_OUTPUT_BYTES
                ),
                artifact_limit=(
                    None
                    if self._workspace is None
                    else self._workspace.artifact_max_bytes
                ),
            )
            if "artifact_store_failed" in answer.output:
                await self._append_event(
                    claimed, RunEventType.WORKSPACE_STORAGE_UNAVAILABLE
                )
            if not answer.failed and changes_workspace(call.name):
                wrote = True
            results.append(answer)
        return _RoundWork(
            (assistant, CanonicalMessage("tool", tuple(results))),
            wrote,
            wait_seconds,
            tuple(events),
        )

    async def _checkpoint_round(
        self,
        claimed: ClaimedRun,
        handle: _LeaseHandle,
        box: "_Sandbox",
        after: ExecutionContext,
        decision: SliceDecision,
        response: ModelResponse,
        executed_ms: int,
        appended: tuple[CanonicalMessage, ...],
        judged: "_Judged",
        events: tuple[ReservedEvent, ...] = (),
    ) -> "_Sandbox | None":
        """One write round's whole consequence: scan, commit, dispose, signal.

        Returns the sandbox (with its new base revision) when the slice
        continues, and None when it is over — however it ended. The round's
        turns and accounting are committed *with* the revision (design §8
        step 5), so the plain `_record` path must not run again for it.
        """
        sandbox = self._sandbox
        runtime = self._workspace
        if sandbox is None or runtime is None:  # pragma: no cover - caller checks
            return None
        service = self._workspace_service(claimed, handle, box)
        # The commit itself keeps the lease (`signal=None`); the round's real
        # signal is applied after the sandbox's fate is confirmed, because a
        # completed Run whose container may still run is not completed.
        slice_command = self._slice_command(
            claimed,
            handle,
            after.state_version,
            SliceDecision(None, limit_reached=decision.limit_reached),
            response,
            executed_ms,
            appended,
            events=events,
            judged=judged,
        )
        try:
            await sandbox.freeze(
                run_id=claimed.run.id, lease_id=handle.lease_id, sandbox_id=box.sandbox_id
            )
        except Exception:
            logger.exception("freeze failed", extra={"run_id": str(claimed.run.id)})
            await self._record(
                claimed,
                handle,
                after.state_version,
                SliceDecision(RunSignal.INTERRUPTED),
                response,
                executed_ms,
                appended=appended,
                events=events,
            )
            return None

        try:
            result = await service.checkpoint(
                WorkspaceCheckpoint(
                    workspace_id=claimed.run.workspace_id,
                    session_id=claimed.run.session_id,
                    run_id=claimed.run.id,
                    base_revision_id=box.revision,
                    quota=runtime.quota,
                    slice_command=slice_command,
                )
            )
        except (LeaseLost, StateVersionConflict):
            handle.lost = True
            return None
        except UnsupportedWorkspaceEntry as refused:
            await self._roll_back_round(
                claimed, handle, box, after, response, executed_ms, appended,
                reason="workspace_entry_not_supported",
                event=RunEventType.WORKSPACE_ENTRY_NOT_SUPPORTED,
                payload={"entry_type": str(refused)[:80]},
                target=None,
            )
            return None
        except Exception:
            logger.exception(
                "workspace checkpoint unavailable", extra={"run_id": str(claimed.run.id)}
            )
            await self._roll_back_round(
                claimed, handle, box, after, response, executed_ms, appended,
                reason="workspace_checkpoint_failed",
                event=RunEventType.WORKSPACE_STORAGE_UNAVAILABLE,
                payload={},
                target=None,
            )
            return None

        measured = result.measurement
        logger.info(
            "workspace checkpoint decided: run=%s status=%s total_bytes=%s quota_bytes=%s",
            claimed.run.id,
            result.status.value,
            None if measured is None else measured.total_bytes,
            runtime.quota.max_bytes,
        )
        if result.status is CheckpointStatus.UNCHANGED:
            # Nothing was committed, so nothing recorded the round either: the
            # turns and accounting go through the ordinary path, or the next
            # round rebuilds a conversation without this one's result and the
            # model repeats the command forever.
            final = await self._dispose_frozen(claimed, handle, box, decision)
            written = await self._record(
                claimed,
                handle,
                after.state_version,
                final,
                response,
                executed_ms,
                appended=appended,
                events=events,
                judged=judged,
            )
            if written is False or final.signal is not None:
                return None
            return box

        if result.status is CheckpointStatus.COMMITTED:
            renewed = replace(box, revision=result.revision_id)
            return await self._dispose_after_commit(claimed, handle, renewed, decision)

        if result.status is CheckpointStatus.LIMIT_EXCEEDED:
            measured = result.measurement
            await self._roll_back_round(
                claimed, handle, box, after, response, executed_ms, appended,
                reason="workspace_limit_exceeded",
                event=RunEventType.WORKSPACE_LIMIT_EXCEEDED,
                # The dimension and numbers, never a filename (design §9).
                payload={
                    "dimension": measured.dimension if measured else None,
                    "total_bytes": measured.total_bytes if measured else None,
                    "object_count": measured.object_count if measured else None,
                },
                target=WorkspaceCleanupTarget.PAUSED_LIMIT,
                confirm_signal=RunSignal.LIMIT_CLEANUP_CONFIRMED,
                confirm_reason=PauseReason.LIMIT,
            )
            return None

        if result.status is CheckpointStatus.CONFLICT:
            await self._roll_back_round(
                claimed, handle, box, after, response, executed_ms, appended,
                reason="workspace_conflict",
                event=RunEventType.WORKSPACE_CONFLICT,
                payload={},
                target=WorkspaceCleanupTarget.FAILED_CONFLICT,
                confirm_signal=RunSignal.RECOVERY_FAILED,
                confirm_reason=None,
            )
            return None

        # STORAGE_FAILED: the dirty sandbox is rolled back and the Run is
        # interrupted for bounded recovery (design §8).
        await self._roll_back_round(
            claimed, handle, box, after, response, executed_ms, appended,
            reason="workspace_checkpoint_failed",
            event=RunEventType.WORKSPACE_CHECKPOINT_FAILED,
            payload={},
            target=None,
        )
        return None

    async def _dispose_frozen(
        self,
        claimed: ClaimedRun,
        handle: _LeaseHandle,
        box: "_Sandbox",
        decision: SliceDecision,
    ) -> SliceDecision:
        """The frozen instance's fate for a round the commit did not record.

        Mirrors `_close_sandbox` without the freeze (the checkpoint already
        froze); a failure downgrades the decision to INTERRUPTED, exactly as
        3B's rule demands.
        """
        sandbox = self._sandbox
        if sandbox is None:  # pragma: no cover - caller checks
            return decision
        try:
            if decision.signal is None:
                await sandbox.thaw(
                    run_id=claimed.run.id,
                    lease_id=handle.lease_id,
                    sandbox_id=box.sandbox_id,
                )
            elif decision.signal is RunSignal.SLICE_ENDED:
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
            logger.exception("sandbox close failed", extra={"run_id": str(claimed.run.id)})
            return SliceDecision(RunSignal.INTERRUPTED)
        return decision

    async def _dispose_after_commit(
        self,
        claimed: ClaimedRun,
        handle: _LeaseHandle,
        box: "_Sandbox",
        decision: SliceDecision,
    ) -> "_Sandbox | None":
        """The frozen instance's fate, then the round's real signal."""
        sandbox = self._sandbox
        if sandbox is None:  # pragma: no cover - caller checks
            return None
        try:
            if decision.signal is None:
                await sandbox.thaw(
                    run_id=claimed.run.id,
                    lease_id=handle.lease_id,
                    sandbox_id=box.sandbox_id,
                )
                return box
            if decision.signal is RunSignal.SLICE_ENDED:
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
            logger.exception("sandbox close failed", extra={"run_id": str(claimed.run.id)})
            await self._apply(claimed, RunSignal.INTERRUPTED)
            return None

        await self._apply(claimed, decision.signal, pause_reason=decision.pause_reason)
        if decision.signal is RunSignal.SAFE_CANCEL_STARTED:
            # The sandbox is already gone, so the cancellation completes here.
            await self._apply(claimed, RunSignal.SAFE_CANCEL_FINISHED)
        return None

    async def _roll_back_round(
        self,
        claimed: ClaimedRun,
        handle: _LeaseHandle,
        box: "_Sandbox",
        after: ExecutionContext,
        response: ModelResponse,
        executed_ms: int,
        appended: tuple[CanonicalMessage, ...],
        *,
        reason: str,
        event: RunEventType,
        payload: dict[str, Any],
        target: "WorkspaceCleanupTarget | None",
        confirm_signal: RunSignal | None = None,
        confirm_reason: PauseReason | None = None,
    ) -> None:
        """Design §9's A1 rollback: the step is undone, and the record says so.

        One transaction writes the rewritten tool results against the
        preceding revision, the interruption, the workspace fact, and — when a
        destination is known — the cleanup intent. Then the sandbox and its
        volume are destroyed, and only a confirmed destruction may move the
        Run onward; an unconfirmed one leaves it interrupted for the
        Scheduler.
        """
        rewritten = _with_rollback_results(appended, reason)
        recorded = await self._record(
            claimed,
            handle,
            after.state_version,
            SliceDecision(RunSignal.INTERRUPTED),
            response,
            executed_ms,
            appended=rewritten,
            events=(ReservedEvent(event_type=event, payload=payload),),
            cleanup_target=target,
            cleanup_sandbox_id=box.sandbox_id if target is not None else None,
        )
        if recorded is False:
            return

        sandbox = self._sandbox
        if sandbox is None:  # pragma: no cover - caller checks
            return
        try:
            # INTERRUPTED already released the WorkerLease. The Controller's
            # Worker `destroy` requires a live one, so that call is
            # `lease_invalid` by construction. The Scheduler's no-lease
            # `cleanup` is the reclaim that can still succeed — and the one
            # the next cycle retries if this attempt is unconfirmed.
            await sandbox.cleanup(
                run_id=claimed.run.id, sandbox_id=box.sandbox_id
            )
        except Exception:
            # Not confirmed, so the Run must not claim a safe pause while the
            # oversized volume may still exist (design §9 step 5).
            logger.exception(
                "rollback destroy unconfirmed", extra={"run_id": str(claimed.run.id)}
            )
            return
        if confirm_signal is not None:
            await self._apply(
                claimed,
                confirm_signal,
                pause_reason=confirm_reason,
                confirmed_sandbox_id=box.sandbox_id,
            )

    async def _apply(
        self,
        claimed: ClaimedRun,
        signal: RunSignal,
        *,
        pause_reason: PauseReason | None = None,
        confirmed_sandbox_id: UUID | None = None,
    ) -> None:
        async with self._sessions.begin() as session:
            await SqlRunStore(session).apply_signal(
                ApplySignalCommand(
                    workspace_id=claimed.run.workspace_id,
                    run_id=claimed.run.id,
                    signal=signal,
                    pause_reason=pause_reason,
                    request_id=f"worker-{self._settings.worker_id}-{signal.value}",
                    capabilities=PLATFORM,
                    confirmed_sandbox_id=confirmed_sandbox_id,
                )
            )

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

    async def _record_planning(self, claimed: ClaimedRun, plan: ContextPlan) -> None:
        """Say what was done to the context before the round that used it.

        Written ahead of the model call rather than with the round's own
        transition: what the planner did is true whatever the call then
        returns, and a round that fails would otherwise erase the only record
        of why the model saw less than the transcript holds.
        """
        for record in plan.trimmed:
            await self._append_event(
                claimed, RunEventType.CONTEXT_TRIMMED, record.payload()
            )
        if plan.compacted is not None:
            await self._append_event(
                claimed, RunEventType.CONTEXT_COMPACTED, plan.compacted.payload()
            )

    async def _overflow(
        self,
        claimed: ClaimedRun,
        handle: _LeaseHandle,
        box: "_Sandbox | None",
        context: ExecutionContext,
        plan: ContextPlan,
    ) -> None:
        """§7.4.2's last line: the originals do not fit, so the Run pauses.

        No provider call is made — which is why the round is recorded with
        `model_calls=0`. Sending a request the endpoint would refuse costs the
        Run a call it never got and tells a reader the model failed, when what
        happened is that the platform declined to ask.

        Nothing is trimmed or compacted on this path, so no event says either
        was. What the pause is about is carried by its reason and by the two
        numbers below, and the transcript still holds every message.
        """
        logger.info(
            "context overflow",
            extra={
                "run_id": str(claimed.run.id),
                "input_estimate": plan.input_estimate,
                "allowance": plan.allowance,
            },
        )
        decision = SliceDecision(RunSignal.SAFE_PAUSE_REACHED, PauseReason.CONTEXT_OVERFLOW)
        if box is not None:
            decision = await self._close_sandbox(claimed, handle, box, decision)
        await self._record(
            claimed,
            handle,
            context.state_version,
            decision,
            _no_round(),
            executed_ms=0,
        )

    async def _append_event(
        self,
        claimed: ClaimedRun,
        kind: RunEventType,
        payload: dict[str, Any] | None = None,
    ) -> None:
        async with self._sessions.begin() as session:
            await SqlRunStore(session).append_events(
                AppendEventsCommand(
                    workspace_id=claimed.run.workspace_id,
                    run_id=claimed.run.id,
                    events=(ReservedEvent(event_type=kind, payload=payload or {}),),
                )
            )

    async def _read_context(
        self, workspace_id: UUID, run_id: UUID
    ) -> ExecutionContext | None:
        async with self._sessions() as session:
            return await SqlRunStore(session).execution_context(workspace_id, run_id)

    def _slice_command(
        self,
        claimed: ClaimedRun,
        handle: _LeaseHandle,
        state_version: int,
        decision: SliceDecision,
        response: ModelResponse,
        executed_ms: int,
        appended: tuple[CanonicalMessage, ...],
        events: tuple[ReservedEvent, ...] = (),
        cleanup_target: "WorkspaceCleanupTarget | None" = None,
        cleanup_sandbox_id: UUID | None = None,
        judged: "_Judged | None" = None,
    ) -> RecordSliceCommand:
        if judged is not None:
            # Every write of a judged round carries the verdict, whichever path
            # got here: the commit that lands a write round, and the plain
            # record that lands every other. A round whose write was rolled
            # back carries none, which is the truth — the verdict did not take.
            events = (*events, _verdict_event(judged))
        return RecordSliceCommand(
            workspace_id=claimed.run.workspace_id,
            run_id=claimed.run.id,
            lease_id=handle.lease_id,
            expected_state_version=state_version,
            signal=decision.signal,
            pause_reason=decision.pause_reason,
            limit_reached=decision.limit_reached,
            wait_kind=decision.wait_kind,
            wait_seconds=decision.wait_seconds,
            checkpoint=_checkpoint(response, judged),
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
            events=events,
            workspace_cleanup_target=cleanup_target,
            workspace_cleanup_sandbox_id=cleanup_sandbox_id,
        )

    async def _record(
        self,
        claimed: ClaimedRun,
        handle: _LeaseHandle,
        state_version: int,
        decision: SliceDecision,
        response: ModelResponse,
        executed_ms: int,
        appended: tuple[CanonicalMessage, ...] = (),
        events: tuple[ReservedEvent, ...] = (),
        cleanup_target: "WorkspaceCleanupTarget | None" = None,
        cleanup_sandbox_id: UUID | None = None,
        judged: "_Judged | None" = None,
    ) -> bool:
        """Persist the round. Returns False when this Worker lost the Run."""
        async with self._lease_lock:
            try:
                async with self._sessions.begin() as session:
                    store = SqlRunStore(session)
                    await store.record_slice(
                        self._slice_command(
                            claimed,
                            handle,
                            state_version,
                            decision,
                            response,
                            executed_ms,
                            appended,
                            events=events,
                            cleanup_target=cleanup_target,
                            cleanup_sandbox_id=cleanup_sandbox_id,
                            judged=judged,
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


def _streamer_of(sandbox: SandboxSession) -> StreamedCommandRunner | None:
    """The socket client's stream seam, not the Controller's authorize-only ticket.

    `SandboxController.execute_stream` shares the name and returns a ticket;
    calling it as if it took a sink would fail. The Worker's fakes that still
    buffer through `execute` simply omit `sink` and stay on that path.
    """
    method = getattr(sandbox, "execute_stream", None)
    if method is None:
        return None
    try:
        if "sink" not in inspect.signature(method).parameters:
            return None
    except (TypeError, ValueError):
        return None
    return cast(StreamedCommandRunner, sandbox)


def _with_rollback_results(
    appended: tuple[CanonicalMessage, ...], reason: str
) -> tuple[CanonicalMessage, ...]:
    """The round's turns, with every tool answer replaced by the rollback.

    Design §9 step 3: each call receives a result naming the rollback and its
    own call ID, so resume tells the model the command was rolled back instead
    of leaving an open call or replaying it as if nothing happened.
    """
    rewritten: list[CanonicalMessage] = []
    for message in appended:
        if message.role != "tool":
            rewritten.append(message)
            continue
        replaced = tuple(
            ToolResultBlock(
                call_id=block.call_id,
                output=f"rolled back: {reason}",
                exit_code=126,
                failed=True,
            )
            if isinstance(block, ToolResultBlock)
            else block
            for block in message.blocks
        )
        rewritten.append(CanonicalMessage("tool", replaced))
    return tuple(rewritten)


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


def _round_index(context: ExecutionContext) -> int:
    """Which round this is, counted across the Run rather than the slice.

    So a scenario that needs a second round still gets one after the Run has
    been re-queued at a slice boundary — and so the number the model is told
    and the number a person reads off the Run are the same number.
    """
    return context.budget.consumed_model_calls + 1


def _summaries(context: ExecutionContext) -> tuple[SkillSummary, ...]:
    """One line per bound skill, in the order the author bound them.

    A skill this Run has already loaded is marked as hit, so the planner knows
    not to take away the summary that explains a document already in the
    conversation.
    """
    loaded = set(context.loaded_skills)
    return tuple(
        SkillSummary(
            name=skill.name,
            text=f"- {skill.name}: {skill.description}",
            loaded=skill.skill_version_id in loaded,
        )
        for skill in context.skills
    )


def _plan(context: ExecutionContext) -> ContextPlan:
    """Decide what this round may send, before it is sent.

    An endpoint that declared no window — the deterministic stand-in — gets a
    plan that fits by construction and changes nothing. That is not a bypass:
    there is no window to plan against, so there is no number this could
    honestly compare the conversation to. The summaries still go out: a
    stand-in with no window is still an Agent whose skills it was bound to.
    """
    summaries = _summaries(context)
    if context.window is None:
        return ContextPlan(
            messages=context.messages,
            fits=True,
            input_estimate=0,
            allowance=0,
            skill_summaries=tuple(item.text for item in summaries),
        )
    return plan_context(
        window=context.window,
        safety_rules=SAFETY_PREAMBLE,
        personality=context.spec.personality,
        tool_schemas=tuple(schemas_for(context.spec.tools)),
        history=context.history,
        skill_summaries=summaries,
        segments=(context.spec.context_budget or ContextBudget()).resolve(),
    )


def _request(
    context: ExecutionContext, box: "_Sandbox | None", plan: ContextPlan
) -> ModelRequest:
    """Build one round's request.

    The messages come from the plan, never from the context: the planner is the
    only thing that decides what one round sends, and a caller that reached
    past it would send a request the window was never measured against.
    """
    return ModelRequest(
        policy=context.spec.model_policy,
        personality=context.spec.personality,
        messages=plan.messages,
        round_index=_round_index(context),
        tools=tuple(schemas_for(context.spec.tools)),
        cache_hint=box.hint if box is not None else None,
        skill_summaries=plan.skill_summaries,
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


def _checkpoint(
    response: ModelResponse, judged: "_Judged | None" = None
) -> dict[str, object]:
    """What the round was, in terms a reader of the Run can act on.

    ``usage_quality`` is recorded rather than merely implied by a zero count:
    "nothing was used" and "nobody counted" are different facts, and only one of
    them means the Token limit was meaningfully enforced.

    The round number and the verdict ride here rather than in columns of their
    own for the reason ``failure`` already does: all of it describes one round,
    this is where a round is described, and a column would have to be kept in
    step with it.
    """
    checkpoint: dict[str, object] = {
        "kind": "model_call",
        "stop_reason": response.stop_reason.value,
        "tokens": response.billable_tokens,
        "usage_quality": response.usage_quality.value,
        "failure": response.failure,
    }
    if judged is not None:
        checkpoint["round"] = judged.round
        checkpoint["goal_outcome"] = judged.verdict.outcome.value
        checkpoint["goal_unmet"] = list(judged.verdict.unmet)
    return checkpoint


def _verdict_event(judged: "_Judged") -> ReservedEvent:
    """The judge's answer, on the timeline where a person is watching.

    The instruction is left out: it is derived from ``unmet`` and is already in
    the transcript, where the model that has to act on it will read it.
    """
    return ReservedEvent(
        event_type=RunEventType.GOAL_VERDICT,
        payload={
            "round": judged.round,
            "outcome": judged.verdict.outcome.value,
            "unmet": list(judged.verdict.unmet),
        },
    )
