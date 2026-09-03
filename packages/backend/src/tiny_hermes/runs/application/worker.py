import asyncio
import inspect
import logging
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.agents.domain.models import ContextBudget, EndpointModelPolicy, ModelPolicy
from tiny_hermes.artifacts.application.service import ArtifactLimits, ArtifactRecorder
from tiny_hermes.model_catalog.domain.pricing import (
    CeilingVerdict,
    Cost,
    CostCeiling,
    CostQuality,
    TokenPrices,
    cost_of,
    projected_cost,
    within_ceiling,
)
from tiny_hermes.model_catalog.domain.pricing import unknown as unknown_cost
from tiny_hermes.model_catalog.infrastructure.sql_store import SqlModelEndpointStore
from tiny_hermes.runs.application.images import ImageSource, resolve_images
from tiny_hermes.runs.application.service import LeaseLost, StateVersionConflict
from tiny_hermes.runs.application.tool_answers import (
    answer_agent_delegate,
    answer_artifact_read,
    answer_http_call,
    answer_mcp_call,
    answer_memory_remember,
    answer_platform_tool,
    answer_session_search,
    answer_skill_load,
    answer_skill_propose,
)
from tiny_hermes.runs.domain.context_budget import (
    DEFAULT_COMPACTION_THRESHOLD,
    CompactionRecord,
    ContextPlan,
    CoveredSummary,
    SegmentName,
    SkillSummary,
    plan_context,
)
from tiny_hermes.runs.domain.goal import (
    CompletionCheck,
    GoalEvidence,
    GoalOutcome,
    GoalProposal,
    GoalVerdict,
    judge,
)
from tiny_hermes.runs.domain.models import (
    SAFETY_PREAMBLE,
    Block,
    BudgetSummary,
    CacheStateHint,
    CanonicalMessage,
    CheckpointEffectStatus,
    PauseReason,
    ReasoningBlock,
    RunCapabilities,
    RunEventType,
    RunPurpose,
    RunSignal,
    StoredMessage,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    WorkspaceCleanupTarget,
)
from tiny_hermes.runs.domain.slice_policy import (
    RoundOutcome,
    SliceDecision,
    decide_after_round,
)
from tiny_hermes.runs.domain.summary_prompt import summary_prompt
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.approvals import ApprovalCheck, ApprovalGate
from tiny_hermes.runs.ports.artifacts import ArtifactReads
from tiny_hermes.runs.ports.children import ChildRuns, DelegationWait
from tiny_hermes.runs.ports.http_calls import EgressClaim, HttpToolSender
from tiny_hermes.runs.ports.mcp import BoundMcpTool, McpGateway
from tiny_hermes.runs.ports.memories import MemoryCandidates
from tiny_hermes.runs.ports.model import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
    StopReason,
    UsageQuality,
)
from tiny_hermes.runs.ports.notifier import WakeUpNotifier
from tiny_hermes.runs.ports.proposals import SkillProposals
from tiny_hermes.runs.ports.searches import SessionSearches
from tiny_hermes.runs.ports.skills import SkillLibrary
from tiny_hermes.runs.ports.store import (
    AppendEventsCommand,
    ApplySignalCommand,
    ClaimedRun,
    ClaimRunCommand,
    ExecutionContext,
    RecordSliceCommand,
    RecordSummaryUsageCommand,
    RenewLeaseCommand,
    ReservedEvent,
    StoredSummary,
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
from tiny_hermes.tools.domain.http_calls import HTTP_PREFIX
from tiny_hermes.tools.domain.mcp import (
    MCP_PREFIX,
    estimated_tokens,
    fits_schema_budget,
    schemas_for_tools,
)
from tiny_hermes.tools.domain.openapi import estimated_tokens_of
from tiny_hermes.tools.domain.registry import (
    DEFAULT_OUTPUT_BYTES,
    PLATFORM_TOOLS,
    schemas_for_agent,
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
    #: Set when a call in this round needs a person and did not get one. The
    #: round's turns are discarded rather than appended — see `HttpCallOutcome`
    #: — so a resumed Run asks the model again and the approved call runs then.
    approval: ApprovalCheck | None = None
    #: Facts the round's own tool calls produced, written in the transaction
    #: that records the round. A `skill_loaded` written separately could
    #: survive a rolled-back round, and the next round would then believe it
    #: was holding text that is not in its conversation.
    events: tuple[ReservedEvent, ...] = ()
    #: Set when the round delegated and the children exist. Unlike
    #: `wait_seconds` this is not a request the judge may overrule: the
    #: children are already running and spending the root budget, so the only
    #: question left is whether something outranks waiting for them.
    delegated: DelegationWait | None = None


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
        http_sender: HttpToolSender | None = None,
        approvals: ApprovalGate | None = None,
        mcp: McpGateway | None = None,
        memories: MemoryCandidates | None = None,
        searches: SessionSearches | None = None,
        #: Turns an `ImageBlock.reference` into bytes. `None` on a deployment
        #: with no channel that sends images, which is why a round carrying
        #: none never touches it — see `resolve_images`.
        images: ImageSource | None = None,
        children: ChildRuns | None = None,
        artifacts: ArtifactReads | None = None,
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
        self._http_sender = http_sender
        # Absent, a write that would need a person is refused rather than run:
        # a platform that cannot ask must not decide.
        self._approvals = approvals
        # Absent, a Version that bound MCP tools runs without them and says
        # so, rather than pretending it was never bound any.
        self._mcp = mcp
        # Absent, `memory.remember` is refused rather than silently
        # dropped: a model told nothing would propose the same thing every
        # round, and a deployment with no memory store should say so.
        self._memories = memories
        # Absent, `session.search` is refused rather than answered with
        # nothing: "no past message matched" and "nobody wired the search"
        # are different facts and a model cannot tell them apart.
        self._searches = searches
        self._images = images
        # Absent, `agent.delegate` is refused rather than answered with an
        # empty list of children: a parent told it started nothing and a
        # parent told nobody wired delegation are different situations, and
        # only one of them is worth trying again.
        self._children = children
        # Absent, `artifact.read` is refused rather than answered with
        # nothing: an empty answer reads to a model like an empty file.
        self._artifacts = artifacts
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

    async def _has_waiting_run(self, claimed: ClaimedRun) -> bool:
        """§12.1: would giving up the head right now actually get a message
        handled, rather than just stop this Run?

        Queried fresh every round, at the same cost `_cost_precheck` pays to
        read the budget, rather than cached on the claim: caching would let
        this round's preemption decision rest on last round's facts, and
        "somebody arrived after I started" is exactly the kind of thing that
        can become true partway through a Run that has been going for a
        while.

        `claimed.run.id` is passed because the store asks **two** questions
        with it, about two different Runs: whether *the* successor — the same
        Run `_terminalize` would hand the head to — is claimable, and whether
        *some* `queued` sibling arrived after this Run started. §12.1's
        trigger is the second; the first is what keeps preemption from
        handing the Session to a Run nothing will pick up. Collapsing them
        onto one row is a real bug this rule already had once: see
        `SqlRunStore.has_waiting_run`, which spells out the case it drops.
        `claimed.run.started_at`, not `session_sequence`, is the frame of
        reference: v2.9.1's rule is about when *this* Run began executing.
        """
        async with self._sessions.begin() as session:
            return await SqlRunStore(session).has_waiting_run(
                claimed.run.session_id, claimed.run.id, claimed.run.started_at
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
            mcp = await self._revalidate(claimed, handle, first)
            if mcp is None:
                # The bound subset does not fit the segment that carries it.
                # §16.2: measured, never truncated, so the Run stops before it
                # spends a model call on a tool list it cannot send.
                return
            if any(tool not in PLATFORM_TOOLS for tool in first.tools):
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
                plan = await self._plan_context(claimed, context, mcp)
                if not plan.fits:
                    await self._overflow(claimed, handle, box, context, plan)
                    return
                if plan.changed:
                    await self._record_planning(claimed, plan)
                if claimed.run.purpose is RunPurpose.COMPACTION:
                    # `/compact` 建的那种 Run：压缩已经在 `_plan_context` 里做完
                    # 并记进事件了，这里就该结束——用户没问问题，再调一次模型是
                    # 白花钱。
                    #
                    # 位置在 `_record_planning` 之后、`_cost_precheck` 之前：
                    # 压缩本身那次摘要调用的账已经由 `_bill_summary_call` 记过，
                    # 而这个 Run 不会再产生任何调用，没有第二笔支出需要预检。
                    await self._compaction_done(claimed, handle, box, context)
                    return
                spend = _cost_precheck(context, plan)
                if not spend.allowed:
                    # §12.4: checked before the call, because a limit tested
                    # afterwards is a limit that has already been passed. The
                    # projection uses the largest output the endpoint may
                    # produce, so a valve cannot be talked past by a round that
                    # was going to be cheap.
                    await self._cost_exceeded(claimed, handle, box, context, spend)
                    return
                round_started = monotonic()
                # Before the call, not inside the provider: fetching a
                # channel's image needs that channel's credentials, and a
                # model adapter holding them would be a credential in the
                # wrong module.
                #
                # Failures degrade rather than stopping the round. A Session
                # replays its history, so an image that can never be fetched
                # again would otherwise fail every future Run in that
                # conversation — which it did, to a live one.
                pictures = await resolve_images(
                    plan.messages, self._images, claimed.run.session_id
                )
                response = await self._model.complete(
                    _request(context, box, plan, mcp, pictures)
                )
                if box is not None:
                    # Only the first round of a slice is told, because only the
                    # first one is news.
                    box = replace(box, hint=None)
                executed_ms = int((monotonic() - round_started) * 1000)

                work = await self._answer_tools(
                    claimed, handle, box, response, context, mcp
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
                budget_allows = _budget_after(after, response, executed_ms)
                slice_expired = (
                    monotonic() - started
                ) >= self._settings.max_slice_seconds
                hold_slice = after.compat_deadline_at is not None and not compat_expired
                # §12.1: `_has_waiting_run` opens its own transaction, worth
                # paying only when the answer could change this round's
                # outcome. Probing `decide_after_round` first, with
                # `user_waiting` and `slice_expired` both forced off, answers
                # that by reusing the real priority order rather than
                # duplicating it by hand (a second, hand-copied ordering is
                # exactly the kind of thing that quietly drifts from the
                # first): a signal here means cancel, pause, budget, approval,
                # delegation, the verdict itself or the compat window already
                # decided this round, and the real query would only have been
                # thrown away. `slice_expired` is forced off rather than
                # passed through because it sits *below* `user_waiting` — a
                # round that would otherwise merely end its slice still has
                # to ask, because preemption outranks that too.
                probed = decide_after_round(
                    RoundOutcome(
                        verdict=verdict,
                        approval=work.approval,
                        delegated=work.delegated,
                        cancel_requested=after.cancel_requested,
                        pause_requested=after.pause_requested,
                        budget_allows=budget_allows,
                        slice_expired=False,
                        compat_window_expired=compat_expired,
                        user_waiting=False,
                    )
                )
                # `_has_waiting_run` reads in its own transaction, separate
                # from the `after` read above and the record below — a
                # sibling cancelled in that window is possible and unclosed:
                # this round would still preempt for a message that no
                # longer exists by the time `_terminalize` actually looks.
                # Accepted rather than locked against. Not claiming a bound
                # this doesn't have: whether that read is later a stall
                # (`_terminalize` lands on a `paused` Run once the cancelled
                # one is gone) or clean (the next `queued` Run in line) is
                # `_terminalize`'s own successor-selection outcome, decided
                # fresh at that moment — this race neither causes nor rules
                # out either one. What the race itself is bounded to is
                # narrower: this round's own goal being cut short on
                # information that was already stale by the time it acted.
                waiting = (
                    False if probed.signal is not None else await self._has_waiting_run(claimed)
                )
                decision = decide_after_round(
                    RoundOutcome(
                        verdict=verdict,
                        approval=work.approval,
                        delegated=work.delegated,
                        cancel_requested=after.cancel_requested,
                        pause_requested=after.pause_requested,
                        budget_allows=budget_allows,
                        slice_expired=slice_expired,
                        hold_slice=hold_slice,
                        compat_window_expired=compat_expired,
                        user_waiting=waiting,
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
                    prices=context.prices,
                )
                if written is False or decision.signal is not None:
                    return
        finally:
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)

    async def _revalidate(
        self,
        claimed: ClaimedRun,
        handle: _LeaseHandle,
        context: ExecutionContext,
    ) -> tuple[BoundMcpTool, ...] | None:
        """§16.2's check, once per slice. `None` means the Run must stop.

        Before the Run works rather than during it: a subset measured after the
        first model call would already have been sent, and a remote that
        changed shape mid-slice would give one round a different tool list from
        the next.

        Nothing is charged here. The revalidation makes no model call, so a Run
        that paused on the budget and was resumed is measured again from the
        same place — the earlier attempt cost it nothing to repeat.
        """
        if not context.spec.mcp_tools:
            return ()
        if self._mcp is None:
            await self._append_event(
                claimed,
                RunEventType.MCP_TOOLS_REVALIDATED,
                {"tools": 0, "unreachable": [], "missing": [], "configured": False},
            )
            return ()
        checked = await self._mcp.revalidate(
            context.spec.mcp_tools, _claim_of(claimed)
        )
        if checked.unreachable or checked.missing:
            # Written whenever the subset came back short. A Run that quietly
            # had fewer tools than its Version bound is one whose behaviour
            # changed with nobody publishing anything.
            await self._append_event(
                claimed,
                RunEventType.MCP_TOOLS_REVALIDATED,
                {
                    "tools": len(checked.tools),
                    "unreachable": list(checked.unreachable),
                    "missing": list(checked.missing),
                    "configured": True,
                },
            )
        budget = fits_schema_budget(
            _schema_estimate(context, checked.tools), _schema_allowance(context)
        )
        if budget.fits:
            return checked.tools
        await self._append_event(
            claimed,
            RunEventType.TOOL_SCHEMA_BUDGET_EXCEEDED,
            {"estimate": budget.estimate, "allowance": budget.allowance},
        )
        await self._record(
            claimed,
            handle,
            context.state_version,
            SliceDecision(
                RunSignal.SAFE_PAUSE_REACHED, PauseReason.TOOL_BUDGET_EXCEEDED
            ),
            _no_round(),
            executed_ms=0,
        )
        return None

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
        if any(name.startswith("file.") for name in context.tools):
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
        mcp: tuple[BoundMcpTool, ...] = (),
    ) -> "_RoundWork":
        """Run whatever the round asked for, and build the turns to append.

        ``wrote`` answers design §8's question: may this round have changed
        `/workspace/data`? A refused call never ran, so it cannot have; a
        successful `file.read` looked and touched nothing.
        """
        if response.stop_reason is StopReason.FAILED:
            # A failed round said nothing the transcript should keep.
            return _RoundWork((), False)
        # Kept first in the turn, and kept at all because a thinking endpoint
        # requires its own reasoning handed back on the next request. It is a
        # `ReasoningBlock` rather than text so it never reaches a transcript,
        # a Feishu reply or a completions document — `CanonicalMessage.text`
        # collects only `TextBlock`, and §19.1 keeps internal state off an
        # end-user surface.
        thought: list[Block] = (
            [ReasoningBlock(text=response.reasoning)] if response.reasoning else []
        )
        if response.stop_reason is not StopReason.TOOL_CALL:
            return _RoundWork(
                (
                    CanonicalMessage(
                        "assistant", (*thought, TextBlock(text=response.text))
                    ),
                ),
                False,
            )

        blocks: list[Block] = [*thought]
        if response.text:
            blocks.append(TextBlock(text=response.text))
        blocks.extend(response.tool_calls)
        assistant = CanonicalMessage("assistant", tuple(blocks))

        wrote = False
        wait_seconds: int | None = None
        delegated: DelegationWait | None = None
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
            if call.name == "session.search":
                results.append(
                    await answer_session_search(self._searches, context, call)
                )
                continue
            if call.name == "memory.remember":
                answered, event = await answer_memory_remember(
                    self._memories, context, call
                )
                results.append(answered)
                if event is not None:
                    events.append(event)
                continue
            if call.name == "artifact.read":
                results.append(
                    await answer_artifact_read(self._artifacts, context, call)
                )
                continue
            if call.name == "agent.delegate":
                outcome = await answer_agent_delegate(self._children, context, call)
                results.append(outcome.result)
                if outcome.event is not None:
                    events.append(outcome.event)
                if outcome.wait is not None:
                    # Last one wins, and one is all a round can act on: the Run
                    # enters a single `waiting_external` with a single
                    # deadline, the same as `platform.wait`.
                    delegated = outcome.wait
                continue
            if call.name.startswith(f"{MCP_PREFIX}."):
                # Sent by the platform, like an HTTP tool call and for the same
                # reasons: the credential belongs on this side, and the request
                # leaves through the egress proxy with this Run's layers named.
                outcome = await answer_mcp_call(
                    self._mcp,
                    context,
                    call,
                    mcp,
                    _claim_of(claimed),
                    self._approvals,
                )
                if outcome.event is not None:
                    events.append(outcome.event)
                if outcome.result is None:
                    return _RoundWork((), False, approval=outcome.approval)
                results.append(outcome.result)
                continue
            if call.name.startswith(f"{HTTP_PREFIX}."):
                # Sent by the platform rather than by the sandbox: the
                # credential belongs on this side of the boundary, and the
                # request has to leave through the egress proxy like every
                # other outbound call this process makes.
                outcome = await answer_http_call(
                    self._http_sender,
                    context,
                    call,
                    _claim_of(claimed),
                    self._approvals,
                )
                if outcome.event is not None:
                    events.append(outcome.event)
                if outcome.result is None:
                    # A person has to answer before this call can run. Nothing
                    # this round produced is kept: the Run stops here, and when
                    # it resumes the model is asked from the same history it
                    # had, so the call it makes is the one that was approved.
                    return _RoundWork((), False, approval=outcome.approval)
                results.append(outcome.result)
                continue
            if call.name in PLATFORM_TOOLS:
                # Answered here, never sent down. What this asks for happens to
                # the Run; the Controller has nothing to do with it.
                answered, seconds = answer_platform_tool(call, context.tools)
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
                bound=context.tools,
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
            events=tuple(events),
            delegated=delegated,
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
        #
        # `goal_preempted` must not be neutralized along with it: that flag
        # describes what this round's own verdict and disposition were, not
        # what state the Run transitions to right now, and the interim commit
        # is the checkpoint the Run actually ends on if the sandbox close
        # below never gets a second write. `preempted_decision=decision`
        # keeps the real, un-neutralized disposition available to
        # `_checkpoint` for that one fact, while `signal=None` still keeps
        # the transition itself deferred.
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
            prices=after.prices,
            preempted_decision=decision,
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
            payload = plan.compacted.payload()
            endpoint_id, model = await self._compaction_authorship(claimed, plan.compacted)
            payload["endpoint_id"] = endpoint_id
            payload["model"] = model
            await self._append_event(claimed, RunEventType.CONTEXT_COMPACTED, payload)

    async def _compaction_authorship(
        self, claimed: ClaimedRun, compacted: CompactionRecord
    ) -> tuple[str | None, str | None]:
        """Which endpoint and model wrote this compaction's summary, for the
        `CONTEXT_COMPACTED` event.

        `context_budget.py` has no I/O and cannot resolve this itself (see
        `CompactionRecord.payload`'s comment) — it only knows `source`. A
        `"structural"` record names no endpoint because none was called, and
        it has no row to read either: a structural summary is never persisted,
        which is why `session_compactions` records no source of its own.

        A `"model"` record means `_plan_context` just built this plan from
        the Session's one stored summary row (reused from step 2, or just
        written by `_save_summary` in step 5) — `session_compactions` is
        upserted, never appended, so `latest_summary` reads back exactly
        that row, not some other compaction's. This assumes no concurrent
        compaction for the same Session lands between `_plan_context`
        producing this plan and this call reading it back — nothing in
        either method holds a lock over that window, and a second Run slice
        racing this one could, in principle, replace the row before this
        read.

        Raises if that assumption is wrong: `source == "model"` promising a
        row that is not there. Returning `(None, None)` instead would write
        an event indistinguishable from a `"structural"` one that forgot to
        say so — exactly the ambiguity this event exists to remove — so a
        broken invariant stops the write rather than being papered over
        with a payload that merely looks fine.
        """
        if compacted.source != "model":
            return None, None
        stored = await self._latest_summary(claimed.run.session_id)
        if stored is None:
            raise RuntimeError(
                "compaction record claims source == 'model' but no stored "
                f"summary exists for session {claimed.run.session_id}"
            )
        endpoint_id = str(stored.endpoint_id) if stored.endpoint_id is not None else None
        return endpoint_id, stored.model

    async def _plan_context(
        self,
        claimed: ClaimedRun,
        context: ExecutionContext,
        mcp: tuple[BoundMcpTool, ...] = (),
    ) -> ContextPlan:
        """`_plan`'s decision, widened by the Session's persisted summary.

        Product design §7.4.2: a compaction's summary is model-written,
        generated once and persisted, and every later round reads what was
        saved rather than asking a model again. The order below is that rule
        turned into steps, and the order is the design — not an
        implementation detail free to be rearranged:

        1. Plan once, with no stored summary at all
           (`_plan(context, mcp)`, no I/O). If that plan does not compact,
           there is nothing here to widen — the plan already stands, and nothing
           this round did needed a Session summary read at all.
        2. It compacted, and structurally (`_plan` never passes a stored
           summary in, so "compacted" here always means "structurally" —
           there is no other kind it could produce). `latest_summary` says
           what this Session has already written down, if anything. A
           summary whose `last_sequence` already reaches this round's
           compaction boundary already explains everything the round would
           compact — no model call, just the same plan recomputed with that
           text in hand, standing in for its *own* range rather than this
           round's shorter one (step 6), and it earns the right to replace
           step 1's plan only if `_honestly_widens` agrees. This step spends
           nothing — no model call, only the read already paid for by
           needing to compact at all — so it is never gated on §12.4: a Run
           one round from its ceiling that would have continued on a
           *shorter* reused summary must not be measured against the bigger
           structural estimate instead and paused for no reason.
        3. It does not reach far enough — including "there is no summary
           yet". This is the point past which a model call is actually about
           to be spent, so §12.4 is checked here and nowhere earlier:
           reaching for a stored summary in step 2 costs nothing and must not
           be blocked by it, but paying for a summarization call — plus
           whatever retries a hung endpoint costs — only to be stopped by the
           very same ceiling moments later on the baseline plan it would have
           gotten anyway is spending this Run was never allowed to keep.
        4. Under ceiling. The auxiliary model is asked once, over exactly the
           turns the stored summary has not already digested
           (`summary_prompt`'s update form when one exists, its fresh form
           when none does).
        5. It answered something usable: persisted with `save_summary`
           regardless of what happens next — the row is still a true
           statement about the range it was asked to explain even if this
           particular round cannot use it (a later round, with more room or
           a shorter conversation, still can). It did not answer something
           usable — a timeout, a refusal, an empty response, any exception —
           step 1's plan goes out exactly as it stood. §7.4.2's failure
           ladder, first rung.
        6. Either way — reused from step 2, or just generated and saved in
           step 5 — the summary goes to `plan_context` as a `CoveredSummary`,
           text and range together, and stands in for exactly that range or
           not at all: the boundary is pinned there rather than searched for.
           So the only question left for `_honestly_widens` is whether the
           pinned plan fits, and that is not hypothetical — a model summary
           can be longer than the structural sentence it replaces, and at its
           own boundary it may not fit where the structural one did. Not
           fitting is a generation failure exactly like a timeout: step 1's
           plan goes out unchanged.
        """
        # 先消费 `/compact` 的标记，再规划。读和清在一次语句里（见
        # `take_compaction_request`），所以一个请求只压一次；**压没压成都清掉**
        # ——留着它会让一段短对话背着一个几周后突然生效的请求，而那时解释它的
        # 那条回执早就滚没了。
        forced = await self._take_compaction_request(claimed.run.session_id)

        baseline = _plan(context, mcp, forced=forced)
        if baseline.compacted is None:
            return baseline

        if forced and baseline.compacted.freed_estimate == 0:
            # 免费那一遍已经省不出东西了，模型那一遍更不会：结构摘要
            # （`_summarize`，无模型调用）只写覆盖范围、条数、角色分布和线索词，
            # 永远比模型写的语义摘要短。所以「结构的都没让上下文变小」蕴含
            # 「模型的更不会」——不必先花一次调用才知道。
            #
            # 只对 `forced` 生效。自动那条路上压缩是为了装得下，一次省不出东西
            # 的压缩本来就过不了 `plan.fits`；`forced` 才会不问装不装得下就压，
            # 也只有它会走到这里。
            #
            # 返回不带压缩的那一版，而不是 `baseline`：`baseline` 里那次压缩
            # 自己就是「省下 0」的，采用它等于把上下文变大，并且会记下一条
            # `freed_estimate = 0` 的 `CONTEXT_COMPACTED`——那正是
            # `_honestly_widens` 现在拒绝的东西，两处不能只守一处。
            #
            # 记一条事件：`/compact` 的回执要靠它把「压不动」和「压缩失败」
            # 分开说。不记的话两种都落到「压缩失败，稍后再试一次」——而对这
            # 一种，再试同样不会成。
            await self._append_event(
                claimed,
                RunEventType.CONTEXT_COMPACTION_SKIPPED,
                {
                    "reason": "no_gain",
                    "covered": baseline.compacted.covered,
                    "first_sequence": baseline.compacted.first_sequence,
                    "last_sequence": baseline.compacted.last_sequence,
                },
            )
            return _plan(context, mcp)

        covered_last = baseline.compacted.last_sequence
        stored = await self._latest_summary(claimed.run.session_id)
        if stored is not None and stored.last_sequence >= covered_last:
            candidate = _plan(
                context,
                mcp,
                stored_summary=CoveredSummary(stored.text, stored.last_sequence),
                forced=forced,
            )
            if _honestly_widens(candidate, stored.last_sequence):
                return candidate
            logger.info(
                # Not "did not fit": `plan_context` never returns a compacted
                # plan that failed to fit, so a refusal here is one of two
                # situations this Worker cannot tell apart from the plan in
                # hand — the pinned boundary held nothing that fit, or the
                # covered range left no boundary to stand at (it has moved
                # inside the protected recent history). Naming only the first
                # would send an operator to investigate window sizing for a
                # case that is not about size.
                "a stored summary covered enough by sequence number, but this "
                "round would not compact at the range that summary explains — "
                "either nothing fit there or no boundary sat there. Using the "
                "structural plan for this round",
                extra={"run_id": str(claimed.run.id)},
            )
            return baseline

        # Only past this point does a call actually get made. Gating any
        # earlier — including in front of the free reuse read above — would
        # measure a Run against `baseline`'s estimate when a reused summary
        # could have returned something smaller, the mirror of the Critical
        # this same plan already had to be checked against (see step 6):
        # a Run paused that should have continued. `_execute_slice` runs its
        # own, real check again on whatever this method returns either way —
        # this is an early exit, not a replacement for it.
        if not _cost_precheck(context, baseline).allowed:
            return baseline
        if not _calls_precheck(context.budget):
            # §12.4 withholds the streaming-usage overshoot allowance from
            # the call counter specifically — Token and cost may be crossed
            # by one call's real usage because a streamed response's final
            # usage is only known once the call ends, but a call either
            # happens or it does not, so that excuse does not apply here.
            # The round's own call is the one guaranteed to follow whatever
            # this method returns (`_execute_slice` runs it right after,
            # gated only on cost), so spending this call must leave room for
            # that one too — otherwise a ceiling of `max_model_calls=1`
            # would let a single round spend two calls, the summarizer's and
            # the round's own, before anything noticed.
            return baseline

        generated = await self._generate_summary(claimed, context, baseline.compacted, stored)
        if generated is None:
            return baseline

        await self._save_summary(claimed, context, baseline.compacted, generated)
        candidate = _plan(
            context,
            mcp,
            stored_summary=CoveredSummary(generated, covered_last),
            forced=forced,
        )
        if _honestly_widens(candidate, covered_last):
            return candidate
        logger.warning(
            # Same two indistinguishable situations as the reuse path's log
            # above, and the same reason for not naming just one of them.
            "a freshly generated summary was saved, but this round would not "
            "compact at the range it explains — either nothing fit there or no "
            "boundary sat there. Using the structural plan for this round",
            extra={"run_id": str(claimed.run.id)},
        )
        return baseline

    async def _take_compaction_request(self, session_id: UUID) -> bool:
        """`/compact` 的标记，读走并清掉。自己开一个 session，和
        `_latest_summary` 同一个理由：这一步在规划**之前**，不属于任何一次
        Run 的写事务。

        清掉是在这里而不是等压缩成功之后：留着它会让一段短对话背着一个几周后
        突然生效的请求，而那时解释它的那条回执早就滚没了。
        """
        async with self._sessions() as session:
            taken = await SqlRunStore(session).take_compaction_request(session_id)
            if taken:
                await session.commit()
            return taken

    async def _latest_summary(self, session_id: UUID) -> StoredSummary | None:
        async with self._sessions() as session:
            return await SqlRunStore(session).latest_summary(session_id)

    async def _generate_summary(
        self,
        claimed: ClaimedRun,
        context: ExecutionContext,
        compacted: CompactionRecord,
        stored: StoredSummary | None,
    ) -> str | None:
        """One call to this Run's summary endpoint, over the turns a stored
        summary has not already digested.

        This Agent's own endpoint unless it declared a different one for
        summaries (`_summary_policy`) — the case Task 4 adds.

        `None` on any failure — a non-`completed` stop reason (which is what
        a timeout, a refusal or an empty response all normalize to, see
        `openai_model.normalize`) or an exception the call itself raised — so
        the caller falls back to the structural summary `_plan` already
        produced. §7.4.2's failure ladder, first rung.
        """
        ids = set(compacted.message_ids)
        covered = [item for item in context.history if item.id in ids]
        if stored is not None:
            # The update form: only what the stored summary has not already
            # seen. Re-reading turns it already digested would spend the call
            # summarizing a summary, and `summary_prompt`'s update form exists
            # precisely so that never has to happen.
            covered = [item for item in covered if item.sequence > stored.last_sequence]
        request = ModelRequest(
            policy=_summary_policy(context),
            # Not `context.spec.personality`: this call is a platform
            # operation on the transcript, not the Agent speaking in its own
            # voice, and `summary_prompt` already states everything the model
            # needs to know about the task.
            personality="",
            messages=(
                CanonicalMessage(
                    role="user",
                    blocks=(
                        TextBlock(
                            text=summary_prompt(
                                _transcript_text(covered),
                                stored.text if stored is not None else None,
                            )
                        ),
                    ),
                ),
            ),
            round_index=0,
        )
        try:
            response = await self._model.complete(request)
        except Exception:
            logger.exception(
                "summary generation failed", extra={"run_id": str(claimed.run.id)}
            )
            return None
        # §12.4 applies to this call the same as to any other, whatever it
        # answered — billed before the `stop_reason` check below, since a
        # refusal or a too-small window still spent whatever the provider
        # reports here. Only the exception above skips this, and not because
        # it proves the request "never reached the provider": a timed-out
        # call may well have reached it and been billed by it — this code
        # cannot tell the two apart, and `FailingSummarizer`'s own docstring
        # already calls a timeout "the honest shape" of that exception. What
        # actually gates billing is narrower and true: there is no
        # `ModelResponse` to read usage from. A timed-out call the provider
        # billed and this platform never hears back from is a gap, the same
        # kind `_cost_precheck` already names for streaming — not a
        # guarantee this code makes.
        await self._bill_summary_call(claimed, context, response)
        if response.stop_reason is not StopReason.COMPLETED:
            # As visible as the exception above: a refusal or a window truly
            # too small for the prompt reaches here as an ordinary answer,
            # not a raised error, and was silently indistinguishable from
            # "nothing needed summarizing" before this logged.
            logger.warning(
                "summary generation did not complete: stop_reason=%s",
                response.stop_reason.value,
                extra={"run_id": str(claimed.run.id)},
            )
            return None
        text = response.text.strip()
        if not text:
            logger.warning(
                "summary generation answered with no usable text",
                extra={"run_id": str(claimed.run.id)},
            )
            return None
        return text

    async def _bill_summary_call(
        self, claimed: ClaimedRun, context: ExecutionContext, response: ModelResponse
    ) -> None:
        """Bill one summarization call to the Run-tree's shared budget, and
        record it as its own `CONTEXT_SUMMARY_BILLED` event, in one write.

        Called for every response the caller got back, whatever it reported —
        including one with no usage at all. §12.4 treats the call counter
        beside the token and cost counters as one valve, honoured together,
        not two of three: the call counter is the one that still works on a
        deployment with no price, or no `max_cost`, configured — the default
        shape — and gating it behind "usage was reported" would leave exactly
        that deployment unprotected against a summarizer that never stops
        being asked. `tokens` and `cost` do not need a separate case for "no
        usage" here: `response.billable_tokens` is already `0` and `cost_of`
        already answers `unknown()` when nothing was reported, so passing
        `response`'s raw fields through keeps both honest either way.

        Priced at the summary endpoint's own rate, pinned when — the default,
        no `summary_endpoint_id` declared — that endpoint is this Run's own
        main one: `_summary_policy` then resolves to the main policy
        unchanged, and its price is already fixed in `context.prices`
        (`runs.model_pricing_version_id`, read once at Run creation so a
        later repricing cannot change what an already-running Run is
        charged). Reading a live price for that same endpoint instead would
        let it answer at two different prices within one Run depending only
        on which call asked — the bug this branch exists to not have. Only a
        genuinely different, declared summary endpoint has no such pin to
        read back (`model_pricing_version_id` names one endpoint, the main
        one), so `current_prices_for` — the price in force right now — is
        the only one there is to bill *that* endpoint at.

        A declared summary endpoint with no price configured is accepted at
        publish (`_check_summary_endpoint` checks its window, never its
        price — the same choice this platform already makes for the *main*
        endpoint, which publishes unpriced today too) and bills `unknown()`
        here forever after: §12.4's `_accumulate_cost` turns the whole
        Run-tree's `consumed_cost` permanently unknown the first time any
        round cannot be priced, summary or ordinary, and nothing turns it
        back. A workspace with a cost ceiling then has its very next round
        refused (`within_ceiling` refuses a ceiling that meets an unknown
        cost outright) — visibly, via `RUN_LIMIT_REACHED`, not silently. This
        is the existing rule working as designed on a new source, not a
        special case invented for it.
        """
        main_policy = context.spec.model_policy
        main_endpoint_id = (
            main_policy.endpoint_id
            if isinstance(main_policy, EndpointModelPolicy)
            else None
        )
        policy = _summary_policy(context)
        endpoint_id = (
            policy.endpoint_id if isinstance(policy, EndpointModelPolicy) else None
        )
        async with self._sessions.begin() as session:
            model: str | None = None
            prices: TokenPrices | None = None
            if endpoint_id is not None:
                endpoint = await SqlModelEndpointStore(session).read(endpoint_id)
                if endpoint is not None:
                    model = endpoint.spec.model
                prices = (
                    context.prices
                    if endpoint_id == main_endpoint_id
                    else await SqlRunStore(session).current_prices_for(endpoint_id)
                )
            cost = cost_of(
                prices,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                usage_quality=response.usage_quality,
            )
            await SqlRunStore(session).record_summary_usage(
                RecordSummaryUsageCommand(
                    workspace_id=claimed.run.workspace_id,
                    run_id=claimed.run.id,
                    root_run_id=claimed.run.budget_root_run_id,
                    model_calls=response.model_calls,
                    tokens=response.billable_tokens,
                    cost=cost,
                    event=ReservedEvent(
                        event_type=RunEventType.CONTEXT_SUMMARY_BILLED,
                        payload=_summary_billed_payload(endpoint_id, model, response, cost),
                    ),
                )
            )

    async def _save_summary(
        self,
        claimed: ClaimedRun,
        context: ExecutionContext,
        compacted: CompactionRecord,
        text: str,
    ) -> None:
        """Persist the model's answer, naming which model it was.

        This row is written once and read forever after (§7.4.2) — a
        Session summarized under this call keeps whatever `model` is
        recorded here for as long as the summary is never regenerated, so
        `None` here is not a gap a later pass could fill back in without
        guessing. The read is one more query, in the same transaction as the
        write, and `ModelRouter.complete` already does the identical lookup
        on every ordinary round for the same `endpoint_id` — this is not new
        I/O the platform was avoiding, only I/O this call had not done yet.
        """
        # The same resolution `_generate_summary` used to build the request,
        # not `context.spec.model_policy` directly — a declared summary
        # endpoint means the call above already went to it, and this row
        # would misname the answerer if it looked at the Agent's own policy
        # instead.
        policy = _summary_policy(context)
        endpoint_id = (
            policy.endpoint_id if isinstance(policy, EndpointModelPolicy) else None
        )
        async with self._sessions.begin() as session:
            model: str | None = None
            if endpoint_id is not None:
                endpoint = await SqlModelEndpointStore(session).read(endpoint_id)
                if endpoint is not None:
                    model = endpoint.spec.model
            await SqlRunStore(session).save_summary(
                StoredSummary(
                    session_id=claimed.run.session_id,
                    first_sequence=compacted.first_sequence,
                    last_sequence=compacted.last_sequence,
                    text=text,
                    endpoint_id=endpoint_id,
                    model=model,
                ),
                workspace_id=claimed.run.workspace_id,
            )

    async def _compaction_done(
        self,
        claimed: ClaimedRun,
        handle: Any,
        box: Any,
        context: ExecutionContext,
    ) -> None:
        """把一个只做压缩的 Run 正常结束掉。

        照 `_overflow` 的形状：拼一个 `SliceDecision` 交给 `_record`，让状态机
        自己收尾——而不是在这里直接改状态。区别只在信号是 `COMPLETED` 而不是
        暂停：这个 Run 做完了它被创建时要做的那件事。

        `_no_round()`：这一轮没有模型往返可报。压缩那次调用的用量由
        `_bill_summary_call` 单独记成 `CONTEXT_SUMMARY_BILLED`，不属于这里。
        """
        decision = SliceDecision(RunSignal.COMPLETED, None)
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

    async def _cost_exceeded(
        self,
        claimed: ClaimedRun,
        handle: _LeaseHandle,
        box: "_Sandbox | None",
        context: ExecutionContext,
        verdict: CeilingVerdict,
    ) -> None:
        """The spending valve, reached. No provider call was made.

        Recorded with `model_calls=0` for the same reason a context overflow
        is: sending a request this platform had already decided not to pay for
        would cost the Run a call it never got, and would tell a reader the
        model did something.
        """
        logger.info(
            "cost ceiling reached",
            extra={"run_id": str(claimed.run.id), "reason": verdict.reason},
        )
        await self._append_event(
            claimed, RunEventType.RUN_LIMIT_REACHED, {"valve": "cost", "reason": verdict.reason}
        )
        decision = SliceDecision(
            RunSignal.SAFE_PAUSE_REACHED, PauseReason.LIMIT, limit_reached=True
        )
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
        prices: TokenPrices | None = None,
        preempted_decision: SliceDecision | None = None,
    ) -> RecordSliceCommand:
        # `preempted_decision` defaults to `decision` because in every
        # ordinary call the two questions — "what transition does this write
        # record?" and "was this round's own verdict overridden by
        # preemption?" — have one answer between them. `_checkpoint_round` is
        # the one caller where they differ: its interim commit passes a
        # neutralized `decision` (`signal=None`, so the transition is
        # deferred until the sandbox's fate is confirmed) alongside the real
        # one here, so `goal_preempted` and the timeline still describe what
        # this round's disposition actually was.
        goal_decision = decision if preempted_decision is None else preempted_decision
        if judged is not None:
            # Every write of a judged round carries the verdict, whichever path
            # got here: the commit that lands a write round, and the plain
            # record that lands every other. A round whose write was rolled
            # back carries none, which is the truth — the verdict did not take.
            events = (*events, _verdict_event(judged, _is_preempted(goal_decision, judged)))
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
            wait_policy=decision.wait_policy,
            checkpoint=_checkpoint(response, judged, goal_decision),
            checkpoint_replay_safe=response.replay_safe,
            checkpoint_effect_status=(
                CheckpointEffectStatus.UNKNOWN
                if response.external_effect_unknown
                else CheckpointEffectStatus.NONE
            ),
            executed_ms=executed_ms,
            model_calls=response.model_calls,
            tokens=response.billable_tokens,
            # The correction half of §12.4: what the round actually cost, at
            # the price this Run fixed, from whatever the provider reported.
            # `None` when nothing can be said, which makes the Run's total
            # unknown from here on rather than adding a zero.
            cost=_cost_from(response, prices),
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
        prices: TokenPrices | None = None,
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
                            prices=prices,
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
        for skill in context.granted_skills
    )


def _transcript_text(covered: Sequence[StoredMessage]) -> str:
    """The covered turns, as words the summarizer can read.

    Not `CanonicalMessage.text`: that drops tool calls and their results, and
    a summary that cannot see a shell command or its exit code cannot write
    §7.4.2's 关键事实 or 已作出的决定 sections truthfully — both are exactly
    the kind of fact that only shows up in a tool round, never in a text
    turn either side typed.
    """
    lines: list[str] = []
    for item in covered:
        for block in item.message.blocks:
            if isinstance(block, TextBlock):
                lines.append(f"{item.message.role}: {block.text}")
            elif isinstance(block, ToolCallBlock):
                lines.append(
                    f"{item.message.role} called {block.name}({block.arguments})"
                )
            elif isinstance(block, ToolResultBlock):
                lines.append(f"tool result for {block.call_id}: {block.output}")
    return "\n".join(lines)


def _honestly_widens(plan: ContextPlan, covered_last: int) -> bool:
    """Whether a summary-widened re-plan may replace the structural
    baseline `_plan_context` built it to improve on.

    Four ways it may not, and every one of them is a generation failure
    §7.4.2 already has an answer for — the structural summary the caller
    started with:

    - `plan.fits` is False. A model summary can be longer than the
      structural sentence it replaced, and `plan_context`'s own answer to
      "even the caller-given text did not make this small enough" is
      `paused(context_overflow)` further up the call stack — but the
      structural summary this call started with may still fit fine, and a
      Run that would have continued on it must not be paused because a
      *different*, longer summary text was tried in its place.
    - `plan.compacted` is None. Nothing was compacted with this text at all:
      either the pinned boundary held nothing that fit, or the covered range
      left no boundary to stand at — the sequence is not in this round's
      history, or it has moved inside the protected recent turns. This has to
      be its own check rather than something `plan.fits` implies, because
      since `plan_context` started returning fitting originals when its
      compaction search comes back empty, a plan can now be `fits=True` and
      carry no compaction whatsoever. Accepting one would send the round out
      with the summary silently dropped and no `CONTEXT_COMPACTED` event
      saying so.
    - `plan.compacted.last_sequence` is not exactly `covered_last` — the last
      sequence the summary text handed to this call was asked to explain. A
      record that reaches past it claims turns the summarizer never read; one
      that stops short leaves the model reading a summary of turns it is also
      sent verbatim, and writes an event reporting the shorter range as
      though that were all the text covers.

      Neither is what `plan_context` does any more: given a `CoveredSummary`
      it pins the boundary instead of searching, so the only way this
      comparison can fail is a plan that did not honour the pin. It is kept as
      the corroboration, not as the mechanism — the guarantee lives in the
      pin, and this says so out loud rather than trusting it silently.
    - `plan.compacted.freed_estimate` is 0 — the summary is no shorter than
      what it replaces, so applying it makes the context *bigger*. Fitting is
      not the same question: a longer text can still fit, and the first three
      checks all pass while the round comes out worse than if nothing had been
      compacted at all.

      This is not hypothetical. The first `/compact` this platform ever served
      in production wrote `covered 2, source "model", freed_estimate 0` beside
      a `context_summary_billed` of 355 + 1,372 tokens: a summary longer than
      the two short messages it replaced, applied anyway, reported to the user
      as 「已压缩」. `freed_estimate` is `max(saved, 0)`, so 0 is exactly
      "saved nothing" — the check needs no threshold to compare against and no
      constant anybody would have to justify.
    """
    return (
        plan.fits
        and plan.compacted is not None
        and plan.compacted.last_sequence == covered_last
        and plan.compacted.freed_estimate > 0
    )


def _calls_precheck(budget: BudgetSummary) -> bool:
    """Whether a summarization call may be spent without stranding the
    round's own call — the one guaranteed to follow it in the same
    iteration — with nowhere left under `max_model_calls` to land.

    Unlike `_cost_precheck`, this does not grant §12.4's streaming-usage
    overshoot allowance: that allowance exists because a provider's final
    usage is only known once a call ends, and a ceiling checked against an
    estimate can be crossed by that call's real number. A call has no such
    gap — it either happens or it does not — so §12.4 gives Token and cost
    the excuse and withholds it from the call counter. Reserving room for
    both calls (this one and the round's) is what keeps that counter exact:
    checking only this call in isolation would let it spend the ceiling's
    last slot and leave the round's own call, made right after regardless,
    to overshoot by one.
    """
    return budget.consumed_model_calls + 1 < budget.max_model_calls


def _cost_precheck(context: ExecutionContext, plan: ContextPlan) -> CeilingVerdict:
    """Whether one more round fits under this Run's spending limit.

    A Run with no limit is allowed without asking anything, so a deployment
    that never set one is not made to configure prices it does not need.

    Streaming is the honest gap: a provider's final usage only arrives when the
    round ends, so a single call may pass the ceiling before the platform can
    see that it did. The ceiling stops the *next* one. That is written here,
    in `docs/development.md` and in the console rather than left for somebody
    to discover from a bill.
    """
    budget = context.budget
    if budget.max_cost is None:
        return CeilingVerdict(allowed=True)
    consumed = (
        unknown_cost()
        if budget.consumed_cost is None
        else Cost(
            amount=budget.consumed_cost,
            currency=budget.cost_currency,
            quality=CostQuality(budget.cost_quality),
        )
    )
    projected = projected_cost(
        context.prices,
        input_estimate=plan.input_estimate,
        max_output_tokens=_max_output(context),
    )
    return within_ceiling(
        CostCeiling(max_amount=budget.max_cost, currency=budget.cost_currency),
        consumed,
        projected,
    )


def _summary_policy(context: ExecutionContext) -> ModelPolicy:
    """The policy the summary call actually answers under.

    One function for both the call (`_generate_summary`'s `ModelRequest`) and
    the record of who answered (`_save_summary`), so they cannot drift apart —
    a call routed to the declared summary endpoint that then got logged
    against the main one would be a stored row nobody could trust.

    Swaps in `summary_endpoint_id` when the Agent declared one; `None` keeps
    the Agent's own policy untouched, because AgentCatalog already refused at
    publish any declared endpoint whose window is smaller
    (`_check_summary_endpoint`) — the undeclared default trivially clears the
    same bar since it is the same window compared to itself.
    """
    policy = context.spec.model_policy
    if isinstance(policy, EndpointModelPolicy) and policy.summary_endpoint_id is not None:
        return policy.model_copy(update={"endpoint_id": policy.summary_endpoint_id})
    return policy


def _max_output(context: ExecutionContext) -> int:
    """The largest answer this Run may be charged for.

    The Agent's own cap when it set one, and the window's reserved output
    otherwise. Never a guess at the likely length: the pre-check exists to
    stop the expensive round, and the expensive round is the long one.
    """
    policy = context.spec.model_policy
    if isinstance(policy, EndpointModelPolicy) and policy.max_output_tokens is not None:
        return policy.max_output_tokens
    return 0 if context.window is None else context.window.reserved_output_tokens


def _cost_from(response: ModelResponse, prices: TokenPrices | None = None) -> Cost | None:
    """One round's cost, or `None` when the platform cannot state it."""
    if prices is None:
        return None
    return cost_of(
        prices,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        usage_quality=response.usage_quality,
    )


def _summary_billed_payload(
    endpoint_id: UUID | None, model: str | None, response: ModelResponse, cost: Cost
) -> dict[str, Any]:
    """What `CONTEXT_SUMMARY_BILLED` says: who answered, what it reported,
    and what this platform believes that cost — the facts an operator
    watching `consumed_model_calls` or `consumed_cost` move needs to explain
    a movement nothing else on the Run's timeline accounts for. That includes
    `model_calls` itself, not just the counters it moves: a payload that
    named the tokens and the cost but left the call count to be inferred
    from a hardcoded "one call" in the UI copy would be the counter's own
    movement asserted nowhere the reader watching it could check. Written
    even when `response` reported nothing (`cost` is `unknown()`, the token
    fields are `None`/`0`): the call still moved `consumed_model_calls`, and
    a reader has to be able to tell that apart from a call that moved
    nothing at all, not just from one that also moved money.

    `cost.amount` is written as a string, not the `Decimal` itself: the JSON
    column `RunEventRow.payload` lands on has no encoder for `Decimal`
    (`shared/database.py` sets none), and the API's own convention for a
    cost figure is already a decimal string (`UsageByQualityResponse`).
    """
    return {
        "endpoint_id": str(endpoint_id) if endpoint_id is not None else None,
        "model": model,
        "model_calls": response.model_calls,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "tokens": response.billable_tokens,
        "cost": str(cost.amount) if cost.known else None,
        "cost_currency": cost.currency,
        "cost_quality": cost.quality.value,
    }


def _claim_of(claimed: ClaimedRun) -> EgressClaim:
    return EgressClaim(
        workspace_id=claimed.run.workspace_id,
        agent_version_id=claimed.run.agent_version_id,
        run_id=claimed.run.id,
    )


def _schema_estimate(
    context: ExecutionContext, mcp: tuple[BoundMcpTool, ...]
) -> int:
    """What this Agent's whole tool list costs, MCP and everything else.

    Measured together because the segment carries them together: an MCP subset
    that fits on its own and not beside four HTTP operations does not fit.
    """
    servers = dict.fromkeys(item.server_name for item in mcp)
    mcp_total = sum(
        estimated_tokens(
            server, [item.tool for item in mcp if item.server_name == server]
        )
        for server in servers
    )
    named = estimated_tokens_of(
        [
            schema["function"]
            for schema in schemas_for_agent(
                context.tools, context.granted_operations
            )
        ]
    )
    return mcp_total + named


def _schema_allowance(context: ExecutionContext) -> int:
    """What the segment that carries tool schemas will hold.

    §7.4.2's table as this Agent adjusted it. Read from the same resolved
    budget the planner uses, so an author who raised the segment gets the room
    they asked for here too.
    """
    budget = (context.spec.context_budget or ContextBudget()).resolve()
    ceiling = budget[SegmentName.TOOL_SCHEMAS].max_tokens
    # `None` would mean "whatever is left", which this segment never is.
    return ceiling if ceiling is not None else 0


def _plan(
    context: ExecutionContext,
    mcp: tuple[BoundMcpTool, ...] = (),
    stored_summary: CoveredSummary | None = None,
    *,
    forced: bool = False,
) -> ContextPlan:
    """Decide what this round may send, before it is sent.

    An endpoint that declared no window — the deterministic stand-in — gets a
    plan that fits by construction and changes nothing. That is not a bypass:
    there is no window to plan against, so there is no number this could
    honestly compare the conversation to. The summaries still go out: a
    stand-in with no window is still an Agent whose skills it was bound to —
    and, for the same reason, still a Run whose subject has memories.

    ``stored_summary`` only ever arrives from `_plan_context`, never chosen
    here: this function stays the same one-shot, no-I/O calculation
    `plan_context` itself is, called with `None` for a first, cheap read of
    whether this round compacts at all, and again with a Session's persisted
    summary once `_plan_context` has decided that summary is the one to use.
    """
    summaries = _summaries(context)
    if context.window is None:
        return ContextPlan(
            messages=context.messages,
            fits=True,
            input_estimate=0,
            allowance=0,
            skill_summaries=tuple(item.text for item in summaries),
            memories=tuple(fact.body for fact in context.memories),
        )
    return plan_context(
        window=context.window,
        safety_rules=SAFETY_PREAMBLE,
        personality=context.spec.personality,
        tool_schemas=_tool_schemas(context, mcp),
        history=context.history,
        skill_summaries=summaries,
        memories=[fact.body for fact in context.memories],
        segments=(context.spec.context_budget or ContextBudget()).resolve(),
        stored_summary=stored_summary,
        # `forced` 是 `/compact`：有人明说了要压，那就不再问这一轮花掉了额度的
        # 几成。传 0 而不是绕过 `plan_context`——级联、保护、失败降级全都还要照
        # 原样走一遍，唯一不同的是「够不够线」这个问题不再被问。
        threshold=0.0 if forced else _compaction_threshold(context),
    )


def _compaction_threshold(context: ExecutionContext) -> float:
    """This Agent's ratio trigger, resolved the same way `_schema_allowance`
    resolves a segment ceiling: read from the same `ContextBudget` the
    planner uses, so an author who adjusted it gets the round they asked for.
    """
    budget = context.spec.context_budget
    if budget is None or budget.compaction_threshold is None:
        return DEFAULT_COMPACTION_THRESHOLD
    return budget.compaction_threshold


def _tool_schemas(
    context: ExecutionContext, mcp: tuple[BoundMcpTool, ...] = ()
) -> tuple[dict[str, Any], ...]:
    return tuple(
        schemas_for_agent(context.tools, context.granted_operations)
        + _mcp_schemas(mcp)
    )


def _mcp_schemas(mcp: tuple[BoundMcpTool, ...]) -> list[dict[str, Any]]:
    """One schema list per server, so two servers offering a `search` do
    not fight over the name."""
    schemas: list[dict[str, Any]] = []
    for server in dict.fromkeys(item.server_name for item in mcp):
        schemas.extend(
            schemas_for_tools(
                server, [item.tool for item in mcp if item.server_name == server]
            )
        )
    return schemas


def _request(
    context: ExecutionContext,
    box: "_Sandbox | None",
    plan: ContextPlan,
    mcp: tuple[BoundMcpTool, ...] = (),
    pictures: dict[str, str] | None = None,
) -> ModelRequest:
    """Build one round's request.

    The messages come from the plan, never from the context: the planner is the
    only thing that decides what one round sends, and a caller that reached
    past it would send a request the window was never measured against.
    """
    return ModelRequest(
        images=pictures or {},
        policy=context.spec.model_policy,
        personality=context.spec.personality,
        messages=plan.messages,
        round_index=_round_index(context),
        tools=_tool_schemas(context, mcp),
        cache_hint=box.hint if box is not None else None,
        skill_summaries=plan.skill_summaries,
        # From the plan, not the context: the planner budgets the memory
        # segment and trims lowest-relevance first, so what the model is told
        # is what survived rather than everything that was read.
        memories=plan.memories,
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


def _is_preempted(decision: SliceDecision | None, judged: "_Judged") -> bool:
    """§12.1: this round's own verdict said `continue`, and the platform
    ended the Run anyway.

    Neither fact alone tells a preempted round from an ordinary one. The
    signal alone cannot distinguish "done" from "cut off early" — both end
    the Run the same way — and the verdict alone cannot distinguish
    "continue, preempted" from "continue, going on to the next round" — both
    are `GoalOutcome.CONTINUE`. Only the conjunction says the head was given
    up rather than earned.

    Shared by `_checkpoint`'s `goal_preempted` and `_verdict_event`'s
    `preempted` so the checkpoint and the timeline cannot say two different
    things about the same round — a fact recorded in one place and silently
    absent from the other is the bug this repo keeps having.
    """
    return (
        decision is not None
        and decision.signal is RunSignal.COMPLETED
        and judged.verdict.outcome is GoalOutcome.CONTINUE
    )


def _checkpoint(
    response: ModelResponse,
    judged: "_Judged | None" = None,
    decision: SliceDecision | None = None,
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
        checkpoint["goal_preempted"] = _is_preempted(decision, judged)
    return checkpoint


def _verdict_event(judged: "_Judged", preempted: bool) -> ReservedEvent:
    """The judge's answer, on the timeline where a person is watching.

    The instruction is left out: it is derived from ``unmet`` and is already in
    the transcript, where the model that has to act on it will read it.

    ``preempted`` rides here too (§12.1): the timeline is a read path of its
    own — SSE subscribers and anyone replaying `run_events` see it without
    ever polling `document()` — and until now it carried `{round, outcome,
    unmet}` for every round including the one that ended the Run, so a
    reader watching only the stream had no way to tell a preempted round
    from an ordinary `continue` that was about to get another one.
    """
    return ReservedEvent(
        event_type=RunEventType.GOAL_VERDICT,
        payload={
            "round": judged.round,
            "outcome": judged.verdict.outcome.value,
            "unmet": list(judged.verdict.unmet),
            "preempted": preempted,
        },
    )
