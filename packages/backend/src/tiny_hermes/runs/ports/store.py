from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from tiny_hermes.agents.domain.delegation import DelegationScope
from tiny_hermes.agents.domain.models import AgentSpec
from tiny_hermes.memory.ports.library import RememberedFact
from tiny_hermes.model_catalog.domain.pricing import Cost, TokenPrices
from tiny_hermes.runs.domain.context_budget import ContextWindow
from tiny_hermes.runs.domain.models import (
    BoundSkill,
    BudgetSummary,
    CallerIdentity,
    CallerType,
    CanonicalMessage,
    CheckpointEffectStatus,
    PauseReason,
    RunCapabilities,
    RunEvent,
    RunEventType,
    RunSignal,
    RunSnapshot,
    RunTree,
    SessionMode,
    SessionSnapshot,
    StoredMessage,
    UnfinishedWork,
    WaitPolicy,
    WithdrawScope,
    WorkspaceCleanupTarget,
    WorkspaceUsageSummary,
)
from tiny_hermes.tenancy.domain.models import Role
from tiny_hermes.tools.domain.http_calls import BoundOperation


@dataclass(frozen=True)
class CreateSessionCommand:
    workspace_id: UUID
    agent_id: UUID
    caller: CallerIdentity
    session_mode: SessionMode
    request_id: str


@dataclass(frozen=True)
class AcceptRunCommand:
    """Everything one idempotent Run acceptance transaction needs."""

    workspace_id: UUID
    session_id: UUID
    caller: CallerIdentity
    capabilities: RunCapabilities
    endpoint: str
    idempotency_key: str
    request_fingerprint: str
    message: CanonicalMessage
    request_id: str
    delivery_mode: str | None = None


@dataclass(frozen=True)
class RetryRunCommand:
    workspace_id: UUID
    source_run_id: UUID
    caller: CallerIdentity
    capabilities: RunCapabilities
    endpoint: str
    idempotency_key: str
    request_fingerprint: str
    request_id: str


@dataclass(frozen=True)
class ControlRunCommand:
    """A user control request; the state machine still chooses the mutation."""

    workspace_id: UUID
    run_id: UUID
    caller: CallerIdentity
    capabilities: RunCapabilities
    signal: RunSignal
    expected_state_version: int
    request_id: str


@dataclass(frozen=True)
class WidenBudgetCommand:
    """Raise one ceiling on a Run's shared budget scope.

    Product design §12.3 requires an explicit act by an authorized subject, so
    this is its own command rather than a field someone can set while doing
    something else. It carries no `consumed_*` value at all: the counters are
    not this operation's business, and a widening that could reset one would be
    a way to defeat the safety valve by re-spending the same budget.
    """

    workspace_id: UUID
    run_id: UUID
    caller: CallerIdentity
    capabilities: RunCapabilities
    expected_state_version: int
    max_model_calls: int
    request_id: str


@dataclass(frozen=True)
class ApplySignalCommand:
    """The one seam future Worker and Scheduler code uses for state signals.

    It never accepts a target state; ``RunStateMachine`` decides.
    """

    workspace_id: UUID
    run_id: UUID
    signal: RunSignal
    request_id: str
    capabilities: RunCapabilities
    expected_state_version: int | None = None
    pause_reason: PauseReason | None = None
    wait_kind: str | None = None
    wait_deadline_at: datetime | None = None
    payload: dict[str, Any] = field(default_factory=dict[str, Any])
    #: For `LIMIT_CLEANUP_CONFIRMED` only: the sandbox the caller confirmed
    #: gone, compared against the Run's recorded cleanup intent.
    confirmed_sandbox_id: UUID | None = None


@dataclass(frozen=True)
class ReservedEvent:
    event_type: RunEventType
    payload: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class AppendEventsCommand:
    """Reserve and write one or more redacted events in one transaction.

    Callers never choose a sequence; the allocator does.
    """

    workspace_id: UUID
    run_id: UUID
    events: tuple[ReservedEvent, ...]


@dataclass(frozen=True)
class RenewLeaseCommand:
    workspace_id: UUID
    run_id: UUID
    lease_id: UUID
    expected_version: int
    lease_seconds: int


@dataclass(frozen=True)
class RecordSliceCommand:
    """One checkpoint, its accounting, its state signal, and the lease release.

    These four are one business operation. Splitting them would let a crash
    leave a released lease beside unaccounted execution time.

    A ``signal`` of ``None`` means the Worker keeps the lease and continues to
    the next round, so only the checkpoint and the accounting are written.
    """

    workspace_id: UUID
    run_id: UUID
    lease_id: UUID
    expected_state_version: int
    signal: RunSignal | None
    pause_reason: PauseReason | None
    limit_reached: bool
    checkpoint: dict[str, Any]
    checkpoint_replay_safe: bool
    checkpoint_effect_status: CheckpointEffectStatus
    executed_ms: int
    model_calls: int
    tokens: int
    request_id: str
    capabilities: RunCapabilities
    #: Whole messages appended to the Session in this transaction: the
    #: assistant's turn, and a tool turn when the round called one. Empty for a
    #: round that failed — the transcript holds what the Agent said, never what
    #: it tried to say.
    #:
    #: Phase 3A carried a single string here, which could not express a round
    #: that acted rather than answered.
    appended: tuple[CanonicalMessage, ...] = ()
    #: Design §6.3: where the Run must go after this exact sandbox and its
    #: volume are confirmed gone. Recorded in the same transaction as the
    #: rollback results, so a crash in between is recoverable from rows.
    workspace_cleanup_target: WorkspaceCleanupTarget | None = None
    workspace_cleanup_sandbox_id: UUID | None = None
    #: Facts written with the slice's own transition (workspace rollbacks name
    #: their reason here). Only written when ``signal`` is not None — a round
    #: that keeps the lease has no transition to attach them to.
    events: tuple["ReservedEvent", ...] = ()
    #: Set only with ``EXTERNAL_WAIT_STARTED``. The store turns the duration
    #: into ``wait_deadline_at`` against the same ``now`` it stamps the rest of
    #: the transition with, so the deadline the Scheduler scans for cannot
    #: predate the row that announced it.
    wait_kind: str | None = None
    wait_seconds: int | None = None
    #: Whether every child must finish or one is enough (§13). Only ever set
    #: beside a `child_runs` wait — a timer and an approval each wait on one
    #: thing, and there is nothing to choose between.
    wait_policy: WaitPolicy | None = None
    #: What this round cost, as `model_catalog.domain.pricing` computed it.
    #: `None` when nothing can be said — no configured price, or an endpoint
    #: that reported no usage — and the store then keeps the Run's total
    #: unknown from here on rather than adding a zero to it.
    cost: Cost | None = None


@dataclass(frozen=True)
class RecordSummaryUsageCommand:
    """One summarization call's usage and cost, billed to its Run-tree's
    shared budget, with the `CONTEXT_SUMMARY_BILLED` event that explains it.

    Deliberately not `RecordSliceCommand`: a summarization call produces no
    `SliceDecision`, appends no transcript turn, and happens inside
    `_plan_context` — before the round it is planning for has even built its
    own request. Bundled with its event in one transaction (like
    `RecordSliceCommand` bundles the checkpoint with its accounting) so a
    crash cannot leave a moved counter with nothing on the timeline saying
    why, or an event on the timeline the counter never actually reflects.
    """

    workspace_id: UUID
    run_id: UUID
    #: `RunRow.budget_root_run_id` — the shared scope, not necessarily this
    #: Run's own id (§13's delegation tree shares one).
    root_run_id: UUID
    #: `ModelResponse.model_calls` — moved for every summarization call that
    #: got a response back, whatever it reported. §12.4, product decision:
    #: the call counter, the token counter and the cost counter are one
    #: valve honoured together, not two of three, and the call counter is
    #: the one that still works on a deployment with no price, or no
    #: `max_cost`, configured — the default shape.
    model_calls: int
    #: What may be added to the shared budget. `ModelResponse.billable_tokens`
    #: — zero when the endpoint's `usage_quality` is `unavailable` or nothing
    #: was reported, never `None`, for the same reason
    #: `RecordSliceCommand.tokens` is an `int`.
    tokens: int
    #: What this platform believes the call cost, always a `Cost` rather than
    #: `Cost | None`: this command is built for every response the caller got
    #: back, not only ones that reported usage, so there is always something
    #: to say — `unknown()` when nothing was reported, same as `cost_of`
    #: already answers on its own.
    cost: Cost
    event: ReservedEvent


@dataclass(frozen=True)
class ExecutionContext:
    """What a Worker needs to run one round, read through the store.

    The Agent configuration comes from the Version the Run fixed at creation,
    never from the Agent's current pointer, so publishing mid-flight cannot
    change how an accepted Run behaves.
    """

    run_id: UUID
    state_version: int
    spec: AgentSpec
    #: The conversation this round is given, oldest first. A persistent Session
    #: hands over everything said in it so far; an ephemeral one hands over only
    #: this Run's own input, which is the first behaviour `session_mode` has
    #: ever had.
    #:
    #: Carried with each message's id and sequence, because a compaction has to
    #: record the range it covered and the ids it stood in for. Anything that
    #: only wants the conversation reads ``messages`` below.
    history: tuple[StoredMessage, ...]
    cancel_requested: bool
    pause_requested: bool
    budget: BudgetSummary
    #: What the endpoint this Run's policy names declared it can take, read at
    #: the same time as the conversation. ``None`` for a policy that names no
    #: endpoint — the deterministic model declares no window, and there is
    #: nothing to plan against.
    window: ContextWindow | None = None
    #: When set, this Run was admitted by Chat Completions and the Worker must
    #: not emit ``SLICE_ENDED`` for an ordinary slice boundary before it.
    compat_deadline_at: datetime | None = None
    #: What the Version bound, in the order the author bound it. Read with the
    #: conversation and the window rather than on demand: what one round sends
    #: is decided at one moment, and a summary read later than the history it
    #: is planned against would be planned against the wrong history.
    skills: tuple[BoundSkill, ...] = ()
    #: The versions this Run has already loaded text from, oldest first. Two
    #: questions come from one fact — how many loads are left, and which
    #: summaries count as 命中 — and reading it as events rather than counting
    #: `skill.load` calls in the transcript keeps it a fact about *this Run*
    #: even when a persistent Session hands over another Run's turns.
    loaded_skills: tuple[UUID, ...] = ()
    #: The HTTP operations this Run's Version bound, assembled from the catalog
    #: at the same moment as the skills and for the same reason. Each carries
    #: everything the call needs — base URL, credential reference, the parsed
    #: operation — so a round composes a request without asking the catalog
    #: anything mid-flight.
    http_operations: tuple[BoundOperation, ...] = ()
    #: What this Run is priced at, fixed when it was created (§12.4). `None`
    #: for a deterministic Agent, for an endpoint nobody has priced, and for a
    #: Run created before anybody entered one — all three mean the same thing
    #: downstream, and none of them means free.
    prices: TokenPrices | None = None
    #: What this Run may be told it remembers: its subject's own private
    #: memories and its Agent's shared ones, read at the same moment as the
    #: conversation. Never anything `pending` — a candidate that reached a
    #: model's context would have been remembered without anybody agreeing.
    memories: tuple[RememberedFact, ...] = ()
    #: How far down the delegation tree this Run sits. `0` unless it was
    #: delegated. Read here rather than looked up when `agent.delegate` is
    #: answered, because §13's third clause has to be decidable from what the
    #: round already holds — a depth fetched separately is a depth that can be
    #: fetched from the wrong Run.
    depth: int = 0
    #: The six faces this Run holds, as they were computed when it was created.
    #: `None` for a Run nobody delegated, which is not the same as one granted
    #: nothing: an empty scope is a child that may do nothing at all.
    delegated_scope: DelegationScope | None = None
    #: Who started the Session this Run belongs to (`_remembered`'s own
    #: subject, read at the same moment). §16.3's `governance` write policy
    #: asks a person; which person is a fact about who is running the Agent,
    #: not about the tool. A `caller_type=end_user` Run asks that end user for
    #: a `user_confirmation` — it is their own write, on their own behalf —
    #: and every other Run still asks the workspace for a `governance_
    #: approval`. `None` only for a Session already gone, the same edge
    #: `_remembered` reads as "no subject" rather than "everybody's".
    caller_type: CallerType | None = None

    @property
    def messages(self) -> tuple[CanonicalMessage, ...]:
        return tuple(item.message for item in self.history)

    @property
    def tools(self) -> tuple[str, ...]:
        """What this Run may actually call, after the delegation narrowed it.

        **Read this, never `spec.tools`.** §13's sixth clause makes a child's
        permission the intersection of its parent's and the delegation's, and
        an intersection computed at one call site is an intersection some other
        call site forgot. Every check the Worker makes goes through here, and
        so does the schema list the model is shown — §10.2's two steps agreeing
        because they read one thing.

        A face nobody named grants nothing: a delegation that lists no tools
        gives a child no tools, even where the child's own Version bound them.
        That is the documented rule rather than a strict reading of it — a
        child should be given what it needs, and a spec that binds `shell.exec`
        for one kind of work does not thereby authorize it for somebody else's.

        An undelegated Run is unnarrowed, which is every Run created before
        this existed and every Run somebody asks for directly.
        """
        if self.delegated_scope is None:
            return self.spec.tools
        return tuple(
            name for name in self.spec.tools if name in self.delegated_scope.tools
        )

    @property
    def granted_skills(self) -> tuple[BoundSkill, ...]:
        """The skills this Run may load, after the same narrowing.

        By version id, which is what the `skills` face carries and what a
        binding is — a name would let a delegation grant a document its author
        never read.
        """
        if self.delegated_scope is None:
            return self.skills
        return tuple(
            skill
            for skill in self.skills
            if str(skill.skill_version_id) in self.delegated_scope.skills
        )

    @property
    def granted_operations(self) -> tuple[BoundOperation, ...]:
        """The HTTP operations this Run may call, narrowed on two faces at once.

        `tools` decides whether the operation may be called at all, by the name
        the model would type. `secrets` decides whether the credential behind
        it may be used, so a child granted the call and not the credential is
        refused rather than sent out unauthenticated — the second is the whole
        reason the two are separate faces.

        An operation needing no credential passes the secrets face, because
        there is no secret being lent.
        """
        if self.delegated_scope is None:
            return self.http_operations
        scope = self.delegated_scope
        return tuple(
            operation
            for operation in self.http_operations
            if operation.call_name in scope.tools
            and (
                operation.credential_ref is None
                or operation.credential_ref in scope.secrets
            )
        )


@dataclass(frozen=True)
class RenewedLease:
    lease_id: UUID
    version: int
    expires_at: datetime


@dataclass(frozen=True)
class RunEventRecord:
    """One retained Run Event as a subscriber receives it."""

    sequence: int
    event_type: RunEventType
    occurred_at: datetime
    payload: dict[str, Any]


@dataclass(frozen=True)
class RunEventWindow:
    """What a subscriber can still be given.

    ``earliest_sequence`` is ``None`` once retention removed every event, so a
    resynchronization hint has to fall back to ``next_sequence``.
    """

    earliest_sequence: int | None
    next_sequence: int
    is_terminal: bool


@dataclass(frozen=True)
class ClaimRunCommand:
    """A claim attempt.

    ``workspace_id`` of ``None`` means any workspace this Worker serves, which
    is what a deployed Worker process wants; a concrete value narrows the
    search for tests and for future per-tenant Workers.
    """

    workspace_id: UUID | None
    worker_id: str
    lease_seconds: int
    request_id: str
    capabilities: RunCapabilities
    session_id: UUID | None = None


@dataclass(frozen=True)
class AcceptedRun:
    """A created or replayed Run plus the exact document the caller receives."""

    run_id: UUID
    document: dict[str, Any]
    replayed: bool


@dataclass(frozen=True)
class ClaimedRun:
    run: RunSnapshot
    lease_id: UUID
    lease_version: int
    lease_expires_at: datetime


@dataclass(frozen=True)
class RepairResult:
    session_id: UUID
    changed: bool
    head_run_id: UUID | None


@dataclass(frozen=True)
class StoredSummary:
    """A Session's one compaction summary, as `session_compactions` holds it.

    `first_sequence`/`last_sequence` name the `session_messages` range this
    summary stands in for — not by id, because §7.4.2 replaces this row
    wholesale on the next compaction and a later summary's range does not
    start where an earlier one's ids left off. Both ends are read:
    `_plan_context` reuses a summary by `last_sequence`, and a withdrawal
    drops one whose range holds a message the user took back
    (`mark_withdrawn`), which needs the near end too.

    Every row here is model-written, so there is no `source` to record: a
    structural summary is not persisted at all — it costs nothing to
    recompute and `plan_context` regenerates it every round from the
    transcript. The distinction an operator needs is on the
    `CONTEXT_COMPACTED` event, where `CompactionRecord.source` states which
    of the two that round's model actually read.
    """

    session_id: UUID
    first_sequence: int
    last_sequence: int
    text: str
    #: Which endpoint wrote it, and under what model name — the two facts
    #: `_record_planning` reads back to name an author on the event. Both are
    #: `None` when the summary call went to a provider that names no endpoint
    #: (the deterministic stand-in), which is a gap in the record, not a
    #: different kind of summary.
    endpoint_id: UUID | None
    model: str | None


class RunStore(Protocol):
    """Run Coordination persistence.

    Every method is one explicit database transaction step. No caller may
    update Run state columns, event sequences, or Session heads directly.
    """

    async def role_for(self, workspace_id: UUID, user_id: UUID) -> Role | None: ...

    async def record_refusal(
        self,
        *,
        workspace_id: UUID,
        actor_type: str,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
    ) -> None:
        """§23 assertion 2's second half: a refusal that leaves a trace.

        Separate from the ordinary audit writers because what it records is
        the *absence* of an action — `result="denied"` rather than
        `succeeded` — and because the caller is, by definition, not
        authorized for the thing being named. A refusal with no record is
        indistinguishable from a request that never happened, which lets the
        platform say "they did not get in" and not "they tried".
        """
        ...

    async def create_session(self, command: CreateSessionCommand) -> SessionSnapshot: ...

    async def get_session(
        self, workspace_id: UUID, session_id: UUID
    ) -> SessionSnapshot | None: ...

    async def list_sessions(self, workspace_id: UUID) -> Sequence[SessionSnapshot]: ...

    async def accept_run(self, command: AcceptRunCommand) -> AcceptedRun: ...

    async def get_run(
        self, workspace_id: UUID, run_id: UUID, capabilities: RunCapabilities
    ) -> RunSnapshot | None: ...

    async def list_runs(
        self, workspace_id: UUID, session_id: UUID | None, capabilities: RunCapabilities
    ) -> Sequence[RunSnapshot]: ...

    async def run_tree(
        self, workspace_id: UUID, run_id: UUID, capabilities: RunCapabilities
    ) -> RunTree | None: ...

    async def control_run(self, command: ControlRunCommand) -> RunSnapshot: ...

    async def widen_budget(self, command: WidenBudgetCommand) -> RunSnapshot: ...

    async def apply_signal(self, command: ApplySignalCommand) -> RunSnapshot: ...

    async def append_events(
        self, command: AppendEventsCommand
    ) -> tuple[RunEvent, ...]: ...

    async def event_window(
        self, workspace_id: UUID, run_id: UUID
    ) -> RunEventWindow | None: ...

    async def list_events_after(
        self, workspace_id: UUID, run_id: UUID, after_sequence: int, limit: int
    ) -> Sequence[RunEventRecord]: ...

    async def claim_head(self, command: ClaimRunCommand) -> ClaimedRun | None: ...

    async def execution_context(
        self, workspace_id: UUID, run_id: UUID
    ) -> ExecutionContext | None: ...

    async def renew_lease(self, command: RenewLeaseCommand) -> RenewedLease | None: ...

    async def record_slice(self, command: RecordSliceCommand) -> RunSnapshot: ...

    async def repair_session_head(
        self, session_id: UUID, request_id: str
    ) -> RepairResult: ...

    async def derive_retry(self, command: RetryRunCommand) -> AcceptedRun: ...

    async def list_session_messages(
        self, workspace_id: UUID, session_id: UUID
    ) -> Sequence[StoredMessage]: ...

    async def usage_summary(self, workspace_id: UUID) -> WorkspaceUsageSummary: ...

    async def record_end_user_session_read(
        self,
        workspace_id: UUID,
        reader: CallerIdentity,
        end_user_id: UUID,
        session_id: UUID,
        request_id: str,
    ) -> None:
        """Design §6: a console read of an end user's message content. Both
        identities go in — the reader and whose conversation it was — because
        "who read X's conversation" is the question this row exists to
        answer, and half the pair can't answer it.
        """
        ...

    async def claim_idempotency(
        self,
        workspace_id: UUID,
        caller_type: CallerType,
        caller_id: UUID,
        endpoint: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> AcceptedRun | None: ...

    async def store_idempotency_response(
        self,
        workspace_id: UUID,
        caller_type: CallerType,
        caller_id: UUID,
        endpoint: str,
        idempotency_key: str,
        run_id: UUID,
        document: dict[str, Any],
    ) -> None: ...

    async def unfinished_work(self, session_id: UUID) -> UnfinishedWork | None:
        """Whether this Session has unfinished work right now, and which kind.

        `"running"` when the head Run is actually executing (or when anything
        unfinished cannot legally be cancelled), `"queued"` when the head is
        already terminal and the unfinished work sits behind it, and
        `"parked"` when the head is stopped on a person or an external event.

        `"parked"` carries `cancellable`: every unfinished Run in the Session,
        in the order they must be cancelled. All of them, not just the head —
        see `UnfinishedWork`.
        """
        ...

    async def has_waiting_run(
        self, session_id: UUID, run_started_at: datetime | None
    ) -> bool:
        """Is there a queued, unfinished Run in this Session created after
        `run_started_at` — the current Run's own `started_at`.

        v2.9.1 narrowed §12.1: the trigger is "arrived after I began
        executing", not "sits behind me in the queue". A burst of messages a
        user sends in one breath each create their own Run (§566), and all of
        them queue before the first one is ever claimed — under the old
        `session_sequence`-behind-me test that whole queue preempted itself,
        so every message but the last got exactly one round.

        `session_sequence` is dropped rather than reused as the bound: it
        encodes *my own* position in the queue at the moment *I* was created,
        which has nothing to say about the moment I actually started
        executing — a Run claimed five minutes after it was created has the
        same `session_sequence` either way. The new question needs a clock
        reading from when I started, and `started_at` is the only column that
        is one; the other Run is compared by its own `created_at`, because
        "did you arrive after that moment" is a time question, not a queue
        position one.

        `None` in means this Run has no `started_at` to compare against — it
        answers `False` rather than guessing, because nothing can honestly be
        said to have arrived "after" a moment that never happened. In
        practice `_has_waiting_run` only ever calls this once a claim has set
        `started_at`, so the `None` branch is a defensive answer, not a path
        the Worker exercises today.
        """
        ...

    async def withdrawable(
        self, session_id: UUID, scope: WithdrawScope, turns: int
    ) -> tuple[list[UUID], int, str]:
        """The row ids a withdrawal of this scope would take, the turn count
        it actually reaches, and the text of the user turn it anchors on.
        """
        ...

    async def mark_withdrawn(
        self, message_ids: Sequence[UUID], *, at: datetime
    ) -> int:
        """Flip `withdrawn_at` on rows that do not have it yet, and drop any
        stored compaction summary whose covered range holds one of them.

        Returns how many rows this call actually changed, which can be lower
        than `len(message_ids)` — a row already withdrawn is left alone.

        The summary goes in the same step, not as a second call the caller has
        to remember: a summary that distilled withdrawn turns is reused whole
        on the next round, and a withdrawal that leaves it standing is one
        §14.3 does not actually make invisible.
        """
        ...

    async def latest_summary(self, session_id: UUID) -> StoredSummary | None:
        """The Session's current compaction summary, or `None` if it has
        never been compacted. Never a history — see `StoredSummary` and
        `save_summary`.
        """
        ...

    async def save_summary(
        self, summary: StoredSummary, *, workspace_id: UUID
    ) -> None:
        """Replace the Session's summary with this one.

        Upserts on `session_id`, per §7.4.2: a second compaction updates the
        first rather than adding beside it, so there is never more than one
        row per Session to read back.
        """
        ...

    async def record_summary_usage(self, command: RecordSummaryUsageCommand) -> None:
        """Bill one summarization call, and say so on the Run's timeline, in
        one transaction. See `RecordSummaryUsageCommand`.
        """
        ...
