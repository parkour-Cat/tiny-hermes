import asyncio
import inspect
import logging
import shlex
from collections.abc import Awaitable, Callable, Mapping, Sequence
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
    answer_todo_write,
    waits_for_approval,
)
from tiny_hermes.runs.domain.context_budget import (
    DEFAULT_COMPACTION_THRESHOLD,
    SUMMARY_TOOL_RESULT_CHARS,
    CompactionRecord,
    ContextPlan,
    CoveredSummary,
    SegmentName,
    SkillSummary,
    estimate_tokens,
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
    RunState,
    StoredMessage,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    WorkspaceCleanupTarget,
    safety_preamble,
)
from tiny_hermes.runs.domain.skill_review import (
    ReviewUnreadable,
    parse_review,
    review_prompt,
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
    PARALLEL_READS,
    PLATFORM_TOOLS,
    UNTRIMMED_TOOLS,
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
    tool_limit_reached: bool = False
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
    #: Which endpoint answered the round — the Agent's own, or the fallback
    #: the Run switched to (§7.4.1). `None` for the deterministic model.
    endpoint_id: UUID | None = None


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
        try:
            await self._review_for_skills(claimed)
        except Exception:
            # §15.4: a suggestion, attempted after the outcome is recorded. It
            # must never be the reason a Worker stops taking Runs.
            logger.exception("skill review failed", extra={"run_id": str(claimed.run.id)})
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
                # §12.1 补充: the boundary — the last round's call and result
                # are recorded, this round's request is not yet built — is the
                # one place an end user's addition can go in without splitting
                # a tool call from its result.
                async with self._sessions.begin() as session:
                    await SqlRunStore(session).absorb_steers(workspace_id, claimed.run.id)
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
                response = await self._complete_recovering(
                    claimed, context, plan, _request(context, box, plan, mcp, pictures)
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
                if verdict.outcome is GoalOutcome.DONE and await self._steer_waiting(claimed):
                    # §12.1 补充: the end user said something while this round
                    # was finishing. Ending here would leave it unread; one
                    # more round, budget permitting, reads it.
                    verdict = GoalVerdict(GoalOutcome.CONTINUE)
                # The number the model was given for this round, not the one
                # the next read would compute: the two differ the moment this
                # round's own model call is counted.
                judged = _Judged(
                    round=_round_index(context),
                    verdict=verdict,
                    endpoint_id=_endpoint_of(after.policy),
                )
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
                budget_allows = (
                    _budget_after(after, response, executed_ms) and not work.tool_limit_reached
                )
                slice_expired = (monotonic() - started) >= self._settings.max_slice_seconds
                rounds_exhausted = _rounds_exhausted(context)
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
                        rounds_exhausted=rounds_exhausted,
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
                        rounds_exhausted=rounds_exhausted,
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
                    # `after`, not `context`: a round a fallback answered is
                    # charged at the fallback's price, which only the read
                    # after the call knows.
                    prices=after.prices,
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
            held = await self._check_holds(
                claimed, handle, box, f"test -e {shlex.quote(f'{DATA_ROOT}/{path}')}", 30
            )
            if held is None:
                return GoalEvidence(declared=True, observable=False)
            checks.append(CompletionCheck(name=path, met=held[0]))

        if condition.verification_command is not None:
            held = await self._check_holds(
                claimed,
                handle,
                box,
                condition.verification_command,
                _VERIFICATION_TIMEOUT_SECONDS,
            )
            if held is None:
                return GoalEvidence(declared=True, observable=False)
            checks.append(
                CompletionCheck(
                    name=condition.verification_command, met=held[0], output=held[1]
                )
            )

        return GoalEvidence(declared=True, checks=tuple(checks))

    async def _check_holds(
        self,
        claimed: ClaimedRun,
        handle: _LeaseHandle,
        box: _Sandbox,
        line: str,
        timeout_seconds: int,
    ) -> tuple[bool, str] | None:
        """Run one check: whether it held, and what it printed. ``None`` means
        it did not answer.

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
        return int(result.exit_code) == 0, str(result.output)

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
        # eight different skills is a Run's ceiling, not a slice's, and a round
        # asking for three must not get three answers out of one round's worth
        # of room.
        loaded = list(context.loaded_skills)
        tool_limit_reached = False

        async def reserve() -> bool:
            nonlocal tool_limit_reached
            if tool_limit_reached:
                return False
            async with self._sessions.begin() as session:
                accepted = await SqlRunStore(session).reserve_tool_call(
                    claimed.run.workspace_id, claimed.run.id, handle.lease_id
                )
            tool_limit_reached = not accepted
            return accepted

        # All or nothing when the round stops for a person: asked of every call
        # before any runs, so a round that waits has no effects to forget.
        # See `waits_for_approval`.
        for call in response.tool_calls:
            waiting = await waits_for_approval(context, call, mcp, self._approvals)
            if waiting is not None:
                return _RoundWork((), False, approval=waiting)

        started: dict[str, float] = {}
        ended: dict[str, float] = {}
        ahead: dict[str, ToolResultBlock] = {}
        for position, call in enumerate(response.tool_calls):
            if call.call_id in ahead:
                # Answered with the rest of its run of reads, below.
                results.append(ahead[call.call_id])
                continue
            started[call.call_id] = monotonic()
            reads = _consecutive_reads(response.tool_calls, position)
            if len(reads) > 1 and box is not None and self._sandbox is not None:
                ahead = await self._read_together(
                    claimed, handle, box, context, reads, reserve, started, ended
                )
                results.append(ahead[call.call_id])
                continue
            if call.name == "todo.write":
                # Before `reserve()`: keeping a list has no effect outside the
                # transcript, and its cost is Token and model calls, which
                # have ceilings of their own (§16.1, v2.12).
                answered, event = answer_todo_write(context, call)
                results.append(answered)
                if event is not None:
                    events.append(event)
                continue
            external = call.name.startswith((f"{MCP_PREFIX}.", f"{HTTP_PREFIX}."))
            # External writes pass their approval gate before spending a call.
            if not external and not await reserve():
                results.append(
                    ToolResultBlock(
                        call_id=call.call_id,
                        output="refused: tool_budget_exceeded",
                        exit_code=126,
                        failed=True,
                    )
                )
                continue
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
                    before_call=reserve,
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
                    before_call=reserve,
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
            answer = await self._in_sandbox(claimed, handle, box, context, call)
            if not answer.failed and changes_workspace(call.name):
                wrote = True
            results.append(answer)
        events.extend(
            _tool_events(response.tool_calls, results, started, monotonic(), ended)
        )
        return _RoundWork(
            (assistant, CanonicalMessage("tool", tuple(results))),
            wrote,
            tool_limit_reached=tool_limit_reached,
            wait_seconds=wait_seconds,
            events=tuple(events),
            delegated=delegated,
        )

    async def _in_sandbox(
        self,
        claimed: ClaimedRun,
        handle: _LeaseHandle,
        box: "_Sandbox",
        context: ExecutionContext,
        call: ToolCallBlock,
    ) -> ToolResultBlock:
        """One call sent down to the Controller, and its answer."""
        if self._sandbox is None:
            # The callers check this first; answered rather than asserted, for
            # the reason the sequential branch gives.
            return ToolResultBlock(
                call_id=call.call_id,
                output="refused: sandbox_unavailable",
                exit_code=126,
                failed=True,
            )
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
                None if self._workspace is None else self._workspace.artifact_max_bytes
            ),
        )
        if "artifact_store_failed" in answer.output:
            await self._append_event(claimed, RunEventType.WORKSPACE_STORAGE_UNAVAILABLE)
        return answer

    async def _read_together(
        self,
        claimed: ClaimedRun,
        handle: _LeaseHandle,
        box: "_Sandbox",
        context: ExecutionContext,
        reads: Sequence[ToolCallBlock],
        reserve: Callable[[], Awaitable[bool]],
        started: dict[str, float],
        ended: dict[str, float],
    ) -> dict[str, ToolResultBlock]:
        """A run of consecutive sandbox reads, sent together.

        Each is charged first, in order, exactly as one at a time would be —
        so a budget that runs out mid-run refuses the same calls it would have
        refused sequentially — and only the charged ones are sent. Answers come
        back keyed by call so the reply's order is kept.
        """
        charged: list[ToolCallBlock] = []
        answers: dict[str, ToolResultBlock] = {}
        for call in reads:
            started[call.call_id] = monotonic()
            if await reserve():
                charged.append(call)
            else:
                answers[call.call_id] = ToolResultBlock(
                    call_id=call.call_id,
                    output="refused: tool_budget_exceeded",
                    exit_code=126,
                    failed=True,
                )
                ended[call.call_id] = monotonic()

        async def one(call: ToolCallBlock) -> None:
            answers[call.call_id] = await self._in_sandbox(claimed, handle, box, context, call)
            ended[call.call_id] = monotonic()

        await asyncio.gather(*(one(call) for call in charged))
        return answers

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
            SliceDecision(
                None,
                limit_reached=decision.limit_reached,
                limit_valve=decision.limit_valve,
            ),
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
        """`_plan`'s decision, built on the Session's stored summary and, when
        this round compacts, improved by a new model-written one.

        Product design §7.4.2 v2.10. The order is the design:

        1. Read the stored summary and plan on it as a checkpoint — summary,
           put-back, and whatever came after it. Reading costs nothing, so it
           is never gated on §12.4: a Run whose full history would project
           over its ceiling must still be able to go out on the checkpoint.
        2. If that plan does not compact, it stands. A `/compact` that found
           nothing worth doing records why, so its receipt can say "nothing
           to do" rather than "failed".
        3. It compacted, structurally (`plan_context` never writes a model
           summary itself — it has no I/O). Only now is a model call about to
           be spent, so §12.4 is checked here and nowhere earlier.
        4. The summarizer is asked once, over exactly the turns this
           compaction covers, in `summary_prompt`'s update form when the plan
           was built on a stored summary.
        5. The answer is re-planned as the new checkpoint and accepted only if
           that plan fits, compacts nothing further, and sends less than the
           round would have without compacting. Only an accepted summary is
           saved: a stored summary is what every later round builds on, so a
           text that does not fit would make every later round not fit
           either. Anything else — a timeout, a refusal, an empty answer, a
           text too long — sends step 3's structural plan, which is not
           persisted, and the next round that compacts asks again.
        """
        # 先消费 `/compact` 的标记，再规划。读和清在一次语句里（见
        # `take_compaction_request`），所以一个请求只压一次；**压没压成都清掉**
        # ——留着它会让一段短对话背着一个几周后突然生效的请求，而那时解释它的
        # 那条回执早就滚没了。
        forced = await self._take_compaction_request(claimed.run.session_id)
        stored = await self._latest_summary(claimed.run.session_id)
        checkpoint = (
            None if stored is None else CoveredSummary(stored.text, stored.last_sequence)
        )

        baseline = _plan(context, mcp, checkpoint, forced=forced)
        if baseline.compacted is None:
            if forced and baseline.compaction_skipped is not None:
                # `/compact` 的回执要靠它把「没什么可压」和「压缩失败」分开说。
                # 不记的话两种都落到「压缩失败，稍后再试一次」——而对这一种，
                # 再试同样不会成。
                await self._append_event(
                    claimed,
                    RunEventType.CONTEXT_COMPACTION_SKIPPED,
                    {"reason": baseline.compaction_skipped},
                )
            return baseline

        if not _cost_precheck(context, baseline).allowed:
            return baseline
        if not _calls_precheck(context.budget):
            # §12.4 withholds the streaming-usage overshoot allowance from
            # the call counter specifically: a call either happens or it does
            # not. The round's own call is the one guaranteed to follow, so
            # spending this call must leave room for that one too.
            return baseline

        previous = stored if baseline.checkpoint is not None else None
        generated = await self._generate_summary(claimed, context, baseline.compacted, previous)
        if generated is None:
            return baseline

        covered_last = baseline.compacted.last_sequence
        candidate = _plan(context, mcp, CoveredSummary(generated, covered_last))
        if not _summary_holds(candidate, baseline):
            logger.warning(
                "a model summary was generated but not used: it did not fit, or "
                "it saved nothing over the uncompacted round. Using the "
                "structural plan for this round; the summary is not saved",
                extra={"run_id": str(claimed.run.id)},
            )
            return baseline

        await self._save_summary(claimed, context, baseline.compacted, generated)
        before = baseline.before_compaction_estimate
        return replace(
            candidate,
            compacted=replace(
                baseline.compacted,
                summary=generated,
                source="model",
                freed_estimate=(
                    before - candidate.input_estimate
                    if before is not None
                    else baseline.compacted.freed_estimate
                ),
            ),
            before_compaction_estimate=before,
        )

    async def _complete_recovering(
        self,
        claimed: ClaimedRun,
        context: ExecutionContext,
        plan: ContextPlan,
        request: ModelRequest,
    ) -> ModelResponse:
        """One round's model call, recovering the replies §7.4.3 lists.

        A malformed tool call, an empty reply, or one cut off inside a tool
        call is asked again with a note saying what was wrong; a plain-text
        reply cut off by the output limit is continued. The notes and the
        half-written text live only in these requests — what the round
        returns, and so what is stored, is one reply. Each extra request is a
        real call, prechecked like one (`_retry_allowed`), and every attempt's
        usage is added into what the round returns.
        """
        attempts: list[ModelResponse] = []
        written: list[str] = []
        retried = 0
        continued = 0
        current = request
        unanswered = 0
        while True:
            response = await self._model.complete(current)
            if response.transient and not attempts:
                # §7.4.1: nothing came back, so nothing is priced yet and
                # another endpoint may take the round. Only before any attempt
                # got a reply — a round is charged at one endpoint's price.
                switched = await self._fall_back(claimed, context, plan, response)
                if switched is not None:
                    context = switched
                    unanswered += response.model_calls
                    request = replace(
                        request,
                        policy=context.policy,
                        messages=_without_reasoning(request.messages),
                    )
                    current = request
                    continue
            attempts.append(response)
            if response.stop_reason is not StopReason.FAILED:
                break
            reason = response.failure or ""
            if response.continuable:
                if continued >= _MAX_CONTINUATIONS:
                    break
            elif reason not in _RETRY_NOTES or retried >= _MAX_RETRIES:
                break
            if not _retry_allowed(context, plan, attempts):
                break
            if response.continuable:
                continued += 1
                written.append(response.text)
                attempt = continued
            else:
                retried += 1
                attempt = retried
            await self._append_event(
                claimed,
                RunEventType.MODEL_ROUND_RETRIED,
                {"reason": reason, "attempt": attempt, "continued": response.continuable},
            )
            base = request.messages
            if written:
                base = (
                    *base,
                    CanonicalMessage(role="assistant", blocks=(TextBlock(text="".join(written)),)),
                )
            note = _CONTINUE_NOTE if response.continuable else _RETRY_NOTES[reason]
            current = replace(
                request,
                messages=(
                    *base,
                    CanonicalMessage(
                        role="user", blocks=(TextBlock(text=note),), author="platform"
                    ),
                ),
            )
        merged = _merged(attempts, "".join(written))
        if unanswered:
            # The calls that reached nobody still count against the call
            # ceiling; they carried no usage, so they add no Token or cost.
            merged = replace(merged, model_calls=merged.model_calls + unanswered)
        return merged

    async def _steer_waiting(self, claimed: ClaimedRun) -> bool:
        async with self._sessions() as session:
            return await SqlRunStore(session).has_pending_steers(claimed.run.id)

    async def _review_for_skills(self, claimed: ClaimedRun) -> None:
        """§15.4: after a Run with enough tool calls completes, ask once
        whether the work is worth a skill, and open a pending proposal if so.

        After the outcome is recorded, so nothing here can delay the answer or
        change how the Run ended. Best effort: a Worker that stops between the
        two never reviews that Run, and nothing tracks that it did not.
        """
        workspace_id = claimed.run.workspace_id
        async with self._sessions() as session:
            store = SqlRunStore(session)
            status = await store.status_of(workspace_id, claimed.run.id)
            if status is not RunState.COMPLETED:
                return
            context = await store.execution_context(workspace_id, claimed.run.id)
            if context is None:
                return
            review = context.spec.skill_review
            if review is None or not review.enabled:
                return
            if await store.count_events(claimed.run.id, RunEventType.SKILL_REVIEW):
                return
            made = await store.count_events(claimed.run.id, RunEventType.TOOL_CALLED)
        if made < review.min_tool_calls:
            return
        prompt = review_prompt(
            _review_transcript(context, claimed.run.id),
            [(skill.name, skill.description) for skill in context.granted_skills],
        )
        tokenizer = None if context.window is None else context.window.tokenizer
        if not _side_call_allowed(context, estimate_tokens(prompt, tokenizer)):
            await self._append_event(
                claimed, RunEventType.SKILL_REVIEW, {"outcome": "skipped", "reason": "budget"}
            )
            return
        response = await self._model.complete(
            ModelRequest(
                policy=_summary_policy(context),
                personality="",
                messages=(CanonicalMessage(role="user", blocks=(TextBlock(text=prompt),)),),
                round_index=0,
            )
        )
        outcome: dict[str, Any] = await self._act_on_review(context, response)
        await self._bill_summary_call(
            claimed, context, response, event_type=RunEventType.SKILL_REVIEW, extra=outcome
        )

    async def _act_on_review(
        self, context: ExecutionContext, response: ModelResponse
    ) -> dict[str, Any]:
        """Open the proposal the review asked for, through the same path
        `skill.propose` takes, and say what happened."""
        if response.stop_reason is not StopReason.COMPLETED:
            return {"outcome": "failed", "reason": response.failure}
        try:
            decided = parse_review(response.text)
        except ReviewUnreadable as unreadable:
            return {"outcome": "unreadable", "reason": str(unreadable)}
        if decided.decision == "none":
            return {"outcome": "none"}
        if decided.skill_md is None:
            return {"outcome": "unreadable", "reason": "no skill_md"}
        base: UUID | None = None
        files: list[tuple[str, str]] = [("SKILL.md", decided.skill_md)]
        if decided.decision == "patch":
            bound = next(
                (item for item in context.granted_skills if item.name == decided.skill), None
            )
            if bound is None:
                return {"outcome": "refused", "reason": "not a skill this agent was given"}
            base = bound.skill_version_id
            if self._skills is not None:
                # The reviewer rewrites SKILL.md only; the rest of the package
                # is carried over so the diff shows one change, not deletions.
                files += [
                    (path, content)
                    for path, content in await self._skills.read_package(base)
                    if path != "SKILL.md"
                ]
        if self._proposals is None:
            return {"outcome": "refused", "reason": "no skill catalog is configured here"}
        opened = await self._proposals.propose(
            run_id=context.run_id, skill_version_id=base, files=files
        )
        if opened.proposal_id is None:
            return {"outcome": "refused", "reason": opened.refusal}
        return {"outcome": "proposed", "proposal_id": str(opened.proposal_id)}

    async def _fall_back(
        self,
        claimed: ClaimedRun,
        context: ExecutionContext,
        plan: ContextPlan,
        failed: ModelResponse,
    ) -> ExecutionContext | None:
        """Move the Run to the next fallback that can take this round, and
        return the context the round continues under — `None` when none can.

        Each one passed over is said on the timeline with its reason, so a
        round that failed with fallbacks configured shows why none answered.
        """
        from_id = _endpoint_of(context.policy)
        for candidate in context.fallbacks_left:
            async with self._sessions() as session:
                route = await SqlRunStore(session).fallback_route(context.spec, candidate)
            moved: ExecutionContext | None = None
            if route is None:
                skipped = "fallback_unavailable"
            elif plan.input_estimate > route[0].input_allowance:
                skipped = "fallback_window_too_small"
            else:
                moved = replace(
                    context, answering_endpoint_id=candidate, window=route[0], prices=route[1]
                )
                skipped = (
                    None if _cost_precheck(moved, plan).allowed else "fallback_over_cost_ceiling"
                )
            if moved is None or skipped is not None:
                await self._append_event(
                    claimed,
                    RunEventType.MODEL_FALLBACK_SKIPPED,
                    {"endpoint_id": str(candidate), "reason": skipped},
                )
                continue
            async with self._sessions.begin() as session:
                await SqlRunStore(session).switch_endpoint(
                    claimed.run.workspace_id,
                    claimed.run.id,
                    candidate,
                    ReservedEvent(
                        event_type=RunEventType.MODEL_FALLBACK_USED,
                        payload={
                            "from": None if from_id is None else str(from_id),
                            "to": str(candidate),
                            "reason": failed.failure,
                        },
                    ),
                )
            return moved
        return None

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
        self,
        claimed: ClaimedRun,
        context: ExecutionContext,
        response: ModelResponse,
        event_type: RunEventType = RunEventType.CONTEXT_SUMMARY_BILLED,
        extra: dict[str, Any] | None = None,
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
        # The endpoint answering this Run, whose price `context.prices` holds:
        # the Agent's own, or the fallback it switched to (§7.4.1).
        main_policy = context.policy
        main_endpoint_id = (
            main_policy.endpoint_id
            if isinstance(main_policy, EndpointModelPolicy)
            else None
        )
        policy = _summary_policy(context)
        endpoint_id = policy.endpoint_id if isinstance(policy, EndpointModelPolicy) else None
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
                cached_input_tokens=response.cached_input_tokens,
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
                        event_type=event_type,
                        payload={
                            **_summary_billed_payload(endpoint_id, model, response, cost),
                            **(extra or {}),
                        },
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
        endpoint_id = policy.endpoint_id if isinstance(policy, EndpointModelPolicy) else None
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
            events = (
                *events,
                _model_round_event(
                    judged.round,
                    response,
                    executed_ms,
                    _cost_from(response, prices),
                    judged.endpoint_id,
                ),
                _verdict_event(judged, _is_preempted(goal_decision, judged)),
            )
        return RecordSliceCommand(
            workspace_id=claimed.run.workspace_id,
            run_id=claimed.run.id,
            lease_id=handle.lease_id,
            expected_state_version=state_version,
            signal=decision.signal,
            pause_reason=decision.pause_reason,
            limit_reached=decision.limit_reached,
            limit_valve=decision.limit_valve,
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


def _persona(context: ExecutionContext) -> str:
    """The Agent's personality, followed by how its claim of being finished
    will be checked.

    A model used to learn the completion conditions only by failing them, and
    `completion.constraints` — documented as "handed to the model" — reached
    no model at all. Said once, up front, in the segment that is sent every
    round and never trimmed. The planner and the request both call this, so
    the text that is charged is the text that is sent.

    The constraints are the author's words and are not checked by anything;
    the sentence says so, so the model does not read them as enforced.
    """
    personality = context.spec.personality
    condition = context.spec.completion
    if condition is None:
        return personality
    lines = ["When you say the task is finished, the platform checks it:"]
    if condition.expected_artifacts:
        listed = ", ".join(condition.expected_artifacts)
        lines.append(f"- these files must exist under {DATA_ROOT}: {listed}")
    if condition.verification_command is not None:
        lines.append(
            f"- this command must succeed in your sandbox: {condition.verification_command}"
        )
    if condition.constraints:
        lines.append(
            "The author of this Agent also set these constraints (nothing checks "
            f"them automatically; respect them): {condition.constraints}"
        )
    if condition.stop_conditions.max_rounds is not None:
        lines.append(
            f"This task may take at most {condition.stop_conditions.max_rounds} rounds."
        )
    return f"{personality}\n\n" + "\n".join(lines)


def _rounds_exhausted(context: ExecutionContext) -> bool:
    """Whether the round being judged is the last `max_rounds` allows.

    Counted per Run (`rounds_judged`), not from `_round_index`: that one is
    the budget tree's model calls, which a child's or a retry's rounds also
    move.
    """
    completion = context.spec.completion
    ceiling = None if completion is None else completion.stop_conditions.max_rounds
    return ceiling is not None and context.rounds_judged + 1 >= ceiling


def _round_index(context: ExecutionContext) -> int:
    """Which round this is, counted across slices rather than within one.

    So a scenario that needs a second round still gets one after the Run has
    been re-queued at a slice boundary — and so the number the model is told
    and the number a person reads off the Run are the same number.

    Counted from the budget, which the whole Run tree shares: a child's or a
    retry's model calls move it too. A per-Run count is `rounds_judged`.
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
                output = block.output
                if len(output) > SUMMARY_TOOL_RESULT_CHARS:
                    # 摘要要知道跑了什么、大致返回了什么，不需要每个字节；全文在
                    # 会话记录里。截短也让摘要调用本身便宜。
                    output = (
                        output[:SUMMARY_TOOL_RESULT_CHARS]
                        + f" […{len(output)} characters in full]"
                    )
                lines.append(f"tool result for {block.call_id}: {output}")
    return "\n".join(lines)


def _summary_holds(candidate: ContextPlan, baseline: ContextPlan) -> bool:
    """Whether the plan built on a new model summary may replace the
    structural one it was generated to improve on (§7.4.2 v2.10).

    - It fits. A model summary can be far longer than the structural sentence
      it replaces; a Run that would have continued on the structural plan
      must not be paused because a longer text was tried in its place.
    - It stands on exactly the range it was asked about (`checkpoint`), and
      compacts nothing further. Anything else would mean the text did not
      become the checkpoint it is about to be saved as.
    - It sends less than the round would have sent without compacting at
      all. A summary no shorter than what it replaces makes the context
      bigger — the first `/compact` in production did exactly that, and
      reported 「已压缩」.
    """
    before = baseline.before_compaction_estimate
    return (
        candidate.fits
        and candidate.compacted is None
        and baseline.compacted is not None
        and candidate.checkpoint == baseline.compacted.last_sequence
        and before is not None
        and candidate.input_estimate < before
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


def _cost_precheck(
    context: ExecutionContext, plan: "ContextPlan | _Estimate"
) -> CeilingVerdict:
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
    policy = context.policy
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
        cached_input_tokens=response.cached_input_tokens,
    )


#: §7.4.3: how many times one round asks again after a malformed, empty or
#: tool-call-truncated reply, and how many times it asks a truncated text
#: reply to continue. Small on purpose: each is a billed call, and a model
#: that fails the same way three times will not be talked out of it.
_MAX_RETRIES = 2
_MAX_CONTINUATIONS = 3

#: What the model is told when it is asked again. Said in the request only;
#: never stored.
_RETRY_NOTES = {
    "malformed_tool_arguments": (
        "Your previous reply could not be used: a tool call's arguments were not "
        "a valid JSON object. Reply again; if you call a tool, give its arguments "
        "as one valid JSON object."
    ),
    "malformed_tool_call": (
        "Your previous reply could not be used: a tool call was missing its id or "
        "name, or was not shaped as a function call. Reply again."
    ),
    "empty_response": "Your previous reply was empty. Reply again.",
    "max_output_reached": (
        "Your previous reply was cut off at the output limit in the middle of a "
        "tool call. Reply again with a smaller call — for example, write a long "
        "file in several parts."
    ),
}
_CONTINUE_NOTE = (
    "Your reply was cut off at the output limit. Continue exactly where it "
    "stopped, without repeating anything you already wrote."
)


def _retry_allowed(
    context: ExecutionContext, plan: ContextPlan, attempts: Sequence[ModelResponse]
) -> bool:
    """Whether one more call may be made this round — §12.4's checks, counting
    what this round's earlier attempts already spent, so a retry never takes
    the ceiling further past than one call could."""
    budget = context.budget
    spent_calls = sum(item.model_calls for item in attempts)
    if budget.consumed_model_calls + spent_calls >= budget.max_model_calls:
        return False
    if budget.max_tokens is not None:
        spent_tokens = sum(item.billable_tokens for item in attempts)
        if budget.consumed_tokens + spent_tokens >= budget.max_tokens:
            return False
    if budget.max_cost is None:
        return True
    consumed = budget.consumed_cost
    if consumed is None:
        return False
    for item in attempts:
        cost = _cost_from(item, context.prices)
        if cost is None or not cost.known or cost.amount is None:
            # A spend this platform cannot state is not one it can prove fits.
            return False
        consumed += cost.amount
    return _cost_precheck(
        replace(context, budget=replace(budget, consumed_cost=consumed)), plan
    ).allowed


def _merged(attempts: Sequence[ModelResponse], written: str) -> ModelResponse:
    """The round's one reply: the last attempt, with every attempt's usage
    added in and any continued text in front of its own.

    Counts are summed only when every attempt reported them; one attempt with
    no usage makes the round's usage unavailable, as §12.4's "unknown is not
    zero" requires.
    """
    final = attempts[-1]
    if len(attempts) == 1:
        return final
    counted = all(
        item.input_tokens is not None
        and item.output_tokens is not None
        and item.usage_quality is not UsageQuality.UNAVAILABLE
        for item in attempts
    )
    cached = [item.cached_input_tokens for item in attempts]
    return replace(
        final,
        text=written + final.text if final.stop_reason is not StopReason.FAILED else final.text,
        model_calls=sum(item.model_calls for item in attempts),
        input_tokens=sum(item.input_tokens or 0 for item in attempts) if counted else None,
        output_tokens=sum(item.output_tokens or 0 for item in attempts) if counted else None,
        usage_quality=final.usage_quality if counted else UsageQuality.UNAVAILABLE,
        cached_input_tokens=(
            sum(value for value in cached if value is not None)
            if counted and all(value is not None for value in cached)
            else None
        ),
        replay_safe=all(item.replay_safe for item in attempts),
        external_effect_unknown=any(item.external_effect_unknown for item in attempts),
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
        "cached_input_tokens": response.cached_input_tokens,
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
    `plan_context` itself is. It is the checkpoint the view is built on —
    the Session's persisted summary, or a freshly generated one being tried
    before it is saved.
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
    schemas = _tool_schemas(context, mcp)
    return plan_context(
        window=context.window,
        # Chosen from the same schema list the request advertises, so the
        # planner charges exactly the preamble the provider sends.
        safety_rules=safety_preamble(tools=bool(schemas)),
        personality=_persona(context),
        tool_schemas=schemas,
        history=context.history,
        skill_summaries=summaries,
        memories=[fact.body for fact in context.memories],
        segments=(context.spec.context_budget or ContextBudget()).resolve(),
        stored_summary=stored_summary,
        threshold=_compaction_threshold(context),
        trigger_cap=_compaction_trigger_cap(context),
        # `/compact`：有人明说了要压，不再问这一轮有没有到触发线。
        forced=forced,
        untrimmed_tools=UNTRIMMED_TOOLS,
    )


def _compaction_trigger_cap(context: ExecutionContext) -> int | None:
    """This Agent's absolute trigger, if it set one. `None` — the default —
    means the ratio alone decides: an Agent on a large window gets to use it."""
    budget = context.spec.context_budget
    return None if budget is None else budget.compaction_trigger_cap_tokens


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
        policy=context.policy,
        personality=_persona(context),
        messages=(
            plan.messages
            if context.answering_endpoint_id is None
            else _without_reasoning(plan.messages)
        ),
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
    if response.cached_input_tokens is not None:
        # How much of the prompt the provider served from its cache — the one
        # number that says whether keeping the prefix stable is working.
        # Absent rather than 0 when the provider did not say.
        checkpoint["cached_input_tokens"] = response.cached_input_tokens
    if judged is not None:
        checkpoint["round"] = judged.round
        checkpoint["goal_outcome"] = judged.verdict.outcome.value
        checkpoint["goal_unmet"] = list(judged.verdict.unmet)
        checkpoint["goal_preempted"] = _is_preempted(decision, judged)
    return checkpoint


def _consecutive_reads(calls: Sequence[ToolCallBlock], position: int) -> list[ToolCallBlock]:
    """The run of `PARALLEL_READS` calls starting at ``position`` — empty when
    the call there is not one."""
    run: list[ToolCallBlock] = []
    for call in calls[position:]:
        if call.name not in PARALLEL_READS:
            break
        run.append(call)
    return run


def _review_transcript(context: ExecutionContext, run_id: UUID) -> str:
    """This Run's own turns, as the summarizer reads them, cut to half the
    window so the review prompt fits: the head (what was asked) and as much of
    the end as there is room for."""
    text = _transcript_text([item for item in context.history if item.source_run_id == run_id])
    if context.window is None:
        return text
    room = context.window.input_allowance // 2
    tokenizer = context.window.tokenizer
    if estimate_tokens(text, tokenizer) <= room:
        return text
    head = text[: len(text) // 10]
    tail = text[len(head) :]
    while tail and estimate_tokens(head + tail, tokenizer) > room:
        tail = tail[len(tail) // 4 :]
    return f"{head}\n[…the middle of the record is not shown…]\n{tail}"


def _side_call_allowed(context: ExecutionContext, input_estimate: int) -> bool:
    """Whether one call outside a round still fits §12.4's valves."""
    budget = context.budget
    if budget.consumed_model_calls + 1 > budget.max_model_calls:
        return False
    if budget.max_tokens is not None and budget.consumed_tokens >= budget.max_tokens:
        return False
    return _cost_precheck(context, _Estimate(input_estimate)).allowed


@dataclass(frozen=True)
class _Estimate:
    """The one thing `_cost_precheck` reads off a plan."""

    input_estimate: int


def _endpoint_of(policy: ModelPolicy) -> UUID | None:
    return policy.endpoint_id if isinstance(policy, EndpointModelPolicy) else None


def _without_reasoning(messages: Sequence[CanonicalMessage]) -> tuple[CanonicalMessage, ...]:
    """The conversation as a fallback is sent it (§7.4.1, v2.12): without the
    main endpoint's reasoning, which only that endpoint reads. Only the
    request is changed; the transcript keeps every block."""
    return tuple(
        replace(message, blocks=kept)
        if len(kept := tuple(b for b in message.blocks if not isinstance(b, ReasoningBlock)))
        != len(message.blocks)
        else message
        for message in messages
    )


def _model_round_event(
    round_number: int,
    response: ModelResponse,
    latency_ms: int,
    cost: Cost | None,
    endpoint_id: UUID | None = None,
) -> ReservedEvent:
    """§11.6's "Token、预计费用和延迟", for one round, on the timeline.

    ``latency_ms`` is measured from the call to the reply (recovery attempts
    included), before any tool ran. ``cost`` is `None` when the round could not
    be priced — the Run's total says unknown for the same reason.
    """
    return ReservedEvent(
        event_type=RunEventType.MODEL_ROUND,
        payload={
            "round": round_number,
            "endpoint_id": None if endpoint_id is None else str(endpoint_id),
            "model_calls": response.model_calls,
            "input_tokens": response.input_tokens,
            "cached_input_tokens": response.cached_input_tokens,
            "output_tokens": response.output_tokens,
            "usage_quality": response.usage_quality.value,
            "cost": str(cost.amount) if cost is not None and cost.known else None,
            "cost_currency": None if cost is None else cost.currency,
            "latency_ms": latency_ms,
            "stop_reason": response.stop_reason.value,
            "failure": response.failure,
        },
    )


def _argument_shape(arguments: Mapping[str, Any]) -> dict[str, str]:
    """Each argument's key and the shape of its value — never the value.

    §19 acceptance item 7 asks for secrets to be stopped on every RunEvent
    serialization path. A summary that holds no value has nothing to stop:
    a token in a `curl` header shows up as `str:58` and nothing else.
    """
    shaped: dict[str, str] = {}
    for key, value in arguments.items():
        if isinstance(value, bool):
            shaped[key] = "bool"
        elif isinstance(value, str):
            shaped[key] = f"str:{len(value)}"
        elif isinstance(value, int | float):
            shaped[key] = "number"
        elif isinstance(value, list):
            shaped[key] = f"list:{len(cast(list[Any], value))}"
        elif isinstance(value, dict):
            shaped[key] = f"object:{len(cast(dict[str, Any], value))}"
        else:
            shaped[key] = "null" if value is None else type(value).__name__
    return shaped


def _tool_events(
    calls: Sequence[ToolCallBlock],
    results: Sequence[Block],
    started: Mapping[str, float],
    finished: float,
    ended: Mapping[str, float] | None = None,
) -> list[ReservedEvent]:
    """§11.6's "工具调用、实际参数摘要、结果和耗时": one event per answered call.

    A call run on its own ended when the next began, and the last when the
    loop did; a call sent with a run of reads recorded its own end (`ended`),
    since those overlap.
    """
    answered = {
        result.call_id: result for result in results if isinstance(result, ToolResultBlock)
    }
    order = [call.call_id for call in calls if call.call_id in started]
    ends = {
        call_id: started[order[index + 1]] if index + 1 < len(order) else finished
        for index, call_id in enumerate(order)
    }
    ends.update(ended or {})
    events: list[ReservedEvent] = []
    for call in calls:
        result = answered.get(call.call_id)
        if result is None or call.call_id not in started:
            continue
        refused = result.exit_code == 126 and result.output.startswith("refused")
        events.append(
            ReservedEvent(
                event_type=RunEventType.TOOL_CALLED,
                payload={
                    "call_id": call.call_id,
                    "tool": call.name,
                    "arguments": _argument_shape(call.arguments),
                    "outcome": "refused" if refused else "failed" if result.failed else "ok",
                    "exit_code": result.exit_code,
                    "output_chars": len(result.output),
                    "duration_ms": int((ends[call.call_id] - started[call.call_id]) * 1000),
                },
            )
        )
    return events


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
