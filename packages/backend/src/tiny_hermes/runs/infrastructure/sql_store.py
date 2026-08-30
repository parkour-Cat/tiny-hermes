import hashlib
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import delete, exists, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from tiny_hermes.agents.domain.delegation import (
    MAX_DELEGATION_DEPTH,
    DelegationScope,
    granted,
)
from tiny_hermes.agents.domain.models import AgentLimits, AgentSpec, EndpointModelPolicy
from tiny_hermes.agents.infrastructure.tables import AgentRow, AgentVersionRow
from tiny_hermes.artifacts.infrastructure.tables import (
    ArtifactGrantRow,
    ArtifactRow,
)
from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.http_tools.infrastructure.documents import operation_from_document
from tiny_hermes.http_tools.infrastructure.tables import (
    HttpToolRow,
    HttpToolVersionRow,
)
from tiny_hermes.memory.domain.scope import scopes_for_run
from tiny_hermes.memory.infrastructure.sql_library import SqlMemoryLibrary
from tiny_hermes.memory.ports.library import RememberedFact
from tiny_hermes.model_catalog.domain.pricing import Cost, CostQuality, TokenPrices
from tiny_hermes.model_catalog.infrastructure.pricing_tables import (
    ModelPricingVersionRow,
)
from tiny_hermes.model_catalog.infrastructure.tables import ModelEndpointRow
from tiny_hermes.runs.application.service import (
    AgentNotPublished,
    BudgetNotWidened,
    DeniedRunControl,
    EventSequenceConflict,
    ForbiddenRunAction,
    IdempotencyKeyReused,
    LeaseLost,
    RetryBudgetExhausted,
    RetryContextStale,
    RetryLimitReached,
    RetryNotSafe,
    RunCoordinationError,
    SessionAgentNotFound,
    StateVersionConflict,
    UnknownRun,
    UnknownSession,
)
from tiny_hermes.runs.domain.approval import ApprovalStatus
from tiny_hermes.runs.domain.context_budget import Accounting, ContextWindow
from tiny_hermes.runs.domain.models import (
    TERMINAL_STATES,
    USAGE_WINDOW,
    BoundSkill,
    BudgetSummary,
    CallerIdentity,
    CallerType,
    CanonicalMessage,
    CheckpointEffectStatus,
    ChildRunRef,
    DeliveryMode,
    PauseReason,
    QueueStatus,
    RunCapabilities,
    RunEvent,
    RunEventType,
    RunSignal,
    RunSnapshot,
    RunState,
    RunStateView,
    RunTree,
    SessionMode,
    SessionSnapshot,
    StateDecision,
    StoppedRun,
    StoredMessage,
    TextBlock,
    TreeNode,
    UnfinishedWork,
    WaitPolicy,
    WithdrawScope,
    WorkspaceCleanupTarget,
    WorkspaceUsageByQuality,
    WorkspaceUsageSummary,
    event_type_for,
    message_from_document,
)
from tiny_hermes.runs.domain.slice_policy import WAIT_CHILD_RUNS
from tiny_hermes.runs.domain.state_machine import (
    TRANSITIONS,
    InvalidStateMetadata,
    InvalidStateTransition,
    RunLimitReached,
    RunStateError,
    RunStateMachine,
)
from tiny_hermes.runs.infrastructure.tables import (
    ApprovalRow,
    IdempotencyRecordRow,
    RunBudgetScopeRow,
    RunEventRow,
    RunRow,
    SessionCompactionRow,
    SessionMessageRow,
    SessionRow,
    WorkerLeaseRow,
)
from tiny_hermes.runs.ports.children import (
    DelegatedChild,
    DelegationRequest,
    DelegationResult,
)
from tiny_hermes.runs.ports.store import (
    AcceptedRun,
    AcceptRunCommand,
    AppendEventsCommand,
    ApplySignalCommand,
    ClaimedRun,
    ClaimRunCommand,
    ControlRunCommand,
    CreateSessionCommand,
    ExecutionContext,
    RecordSliceCommand,
    RecordSummaryUsageCommand,
    RenewedLease,
    RenewLeaseCommand,
    RepairResult,
    ReservedEvent,
    RetryRunCommand,
    RunEventRecord,
    RunEventWindow,
    StoredSummary,
    WidenBudgetCommand,
)
from tiny_hermes.skills.infrastructure.tables import SkillRow, SkillVersionRow
from tiny_hermes.tenancy.domain.models import Role
from tiny_hermes.tenancy.infrastructure.tables import MembershipRow, WorkspaceRow
from tiny_hermes.tools.domain.http_calls import BoundOperation

RESERVE_SEQUENCES = text(
    "UPDATE runs SET next_event_sequence = next_event_sequence + :count "
    "WHERE id = :run_id AND workspace_id = :workspace_id "
    "RETURNING next_event_sequence - :count AS first_sequence"
)

IDEMPOTENCY_RETENTION = timedelta(hours=24)

#: The longest a child's report may be. Long enough for a real answer, short
#: enough that a parent waiting on five of them is not handed a context window
#: full of somebody else's prose — which is the shape §13's seventh clause
#: exists to keep out.
MAX_CHILD_SUMMARY = 4_000

RETRY_ERRORS: dict[str, RunCoordinationError] = {
    "retry_not_safe": RetryNotSafe(),
    "retry_context_stale": RetryContextStale(),
    "retry_budget_exhausted": RetryBudgetExhausted(),
    "retry_limit_reached": RetryLimitReached(),
}

WAITING_HEAD_STATES = frozenset(
    {RunState.PAUSED, RunState.WAITING_APPROVAL, RunState.WAITING_EXTERNAL}
)

#: 同一组状态，写成 `runs.status` 里存的那些字符串。`unfinished_work` 比对的是
#: 列的原值而不是 `RunState`，两边各自 `.value` 一次容易漏，所以只算这一次。
_PARKED_STATUSES = frozenset(state.value for state in WAITING_HEAD_STATES)

#: 能合法收下 `CANCEL_REQUESTED` 的状态，从状态机那张表里推出来而不是另抄一份
#: ——`running` 只收 `SAFE_CANCEL_STARTED`，`cancelling` 一条取消边都没有，抄
#: 一份就等着两边哪天不一样。`unfinished_work` 用它做事前判断：`/new` 要么能
#: 把整个 Session 清干净，要么一个 Run 都不动。
_CANCELLABLE_STATUSES = frozenset(
    state.value
    for (state, signal) in TRANSITIONS
    if signal is RunSignal.CANCEL_REQUESTED
)


#: How many memories one scope may contribute to a round before the planner
#: sees them. A ceiling on the read rather than on the segment: the segment's
#: own budget decides what fits, and this stops a subject with ten thousand
#: memories from being loaded into a process to find out.
MEMORY_READ_LIMIT = 200


class SqlRunStore:
    """PostgreSQL Run Coordination adapter.

    Each public method is one whole business transaction. State columns, event
    sequences, and Session heads change only here, and only as the pure
    ``RunStateMachine`` allows.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._machine = RunStateMachine()

    async def role_for(self, workspace_id: UUID, user_id: UUID) -> Role | None:
        value = await self._session.scalar(
            select(MembershipRow.role).where(
                MembershipRow.workspace_id == workspace_id,
                MembershipRow.user_id == user_id,
            )
        )
        return Role(value) if value else None

    async def record_refusal(
        self,
        *,
        workspace_id: UUID,
        actor_type: str,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
    ) -> None:
        self._session.add(
            AuditEventRow(
                id=uuid4(),
                workspace_id=workspace_id,
                actor_type=actor_type,
                actor_id=actor_id,
                action=action,
                resource_type="session",
                resource_id=resource_id,
                result="denied",
                request_id="",
                context={},
                created_at=datetime.now(UTC),
            )
        )
        await self._session.flush()


    async def create_session(self, command: CreateSessionCommand) -> SessionSnapshot:
        agent = await self._session.scalar(
            select(AgentRow).where(
                AgentRow.id == command.agent_id,
                AgentRow.workspace_id == command.workspace_id,
            )
        )
        if agent is None:
            raise SessionAgentNotFound
        row = SessionRow(
            id=uuid4(),
            workspace_id=command.workspace_id,
            agent_id=command.agent_id,
            session_mode=command.session_mode.value,
            caller_type=command.caller.caller_type.value,
            caller_id=command.caller.caller_id,
            head_run_id=None,
            next_run_sequence=1,
            next_message_sequence=1,
            workspace_revision_id=None,
            created_at=datetime.now(UTC),
        )
        self._session.add(row)
        await self._session.flush()
        self._audit(
            command.workspace_id,
            command.caller.caller_id,
            "session.created",
            "session",
            row.id,
            command.request_id,
            actor_type=command.caller.caller_type.value,
        )
        return _session_snapshot(row)

    async def get_session(
        self, workspace_id: UUID, session_id: UUID
    ) -> SessionSnapshot | None:
        row = await self._session.scalar(
            select(SessionRow).where(
                SessionRow.id == session_id, SessionRow.workspace_id == workspace_id
            )
        )
        return None if row is None else _session_snapshot(row)

    async def list_sessions(self, workspace_id: UUID) -> Sequence[SessionSnapshot]:
        rows = (
            await self._session.scalars(
                select(SessionRow)
                .where(SessionRow.workspace_id == workspace_id)
                .order_by(SessionRow.created_at, SessionRow.id)
            )
        ).all()
        return [_session_snapshot(row) for row in rows]

    async def accept_run(self, command: AcceptRunCommand) -> AcceptedRun:
        replay = await self._claim_idempotency(
            command.workspace_id,
            command.caller.caller_type,
            command.caller.caller_id,
            command.endpoint,
            command.idempotency_key,
            command.request_fingerprint,
        )
        if isinstance(replay, AcceptedRun):
            return replay

        session = await self._lock_session(command.workspace_id, command.session_id)
        if session is None:
            raise UnknownSession
        version_id, limits = await self._published_version(session)

        run_id = uuid4()
        now = datetime.now(UTC)
        # Both read once, here, and copied onto the Run. §12.4: a price
        # correction or a raised ceiling entered tomorrow must not change what
        # a Run that is already running is measured at.
        pricing_version_id = await self._current_pricing(version_id)
        ceiling = await self._cost_ceiling(command.workspace_id)
        run = RunRow(
            id=run_id,
            workspace_id=command.workspace_id,
            session_id=session.id,
            agent_version_id=version_id,
            status=RunState.QUEUED.value,
            state_version=1,
            next_event_sequence=1,
            session_sequence=session.next_run_sequence,
            blocked_by_run_id=session.head_run_id,
            budget_root_run_id=run_id,
            checkpoint_replay_safe=True,
            checkpoint_effect_status=CheckpointEffectStatus.NONE.value,
            checkpoint_workspace_revision_id=session.workspace_revision_id,
            delivery_mode=command.delivery_mode,
            # §16.3's `user_confirmation` may only be answered by the EndUser
            # who started the Run. Through M2 that was only ever the logged-in
            # caller; end-user entry design §5 adds the real subject a
            # `caller_type=end_user` Run confirms to. A ServiceAccount's Run
            # gets none either way, which is why such an Agent has to have
            # chosen a pre-authorization or a governance approval at publish
            # rather than relying on somebody being there.
            end_user_id=(
                command.caller.caller_id
                if command.caller.caller_type in (CallerType.USER, CallerType.END_USER)
                else None
            ),
            model_pricing_version_id=pricing_version_id,
            created_at=now,
            updated_at=now,
        )
        # The Run row must exist before its budget, Session head, and events
        # can reference it, so it is flushed on its own first.
        self._session.add(run)
        await self._session.flush()

        self._session.add(
            SessionMessageRow(
                id=uuid4(),
                session_id=session.id,
                workspace_id=command.workspace_id,
                sequence=session.next_message_sequence,
                role=command.message.role,
                content=command.message.document(),
                source_run_id=run_id,
                redacted=False,
                created_at=now,
            )
        )
        self._session.add(_new_budget(run_id, limits, now, ceiling))
        session.next_run_sequence += 1
        session.next_message_sequence += 1
        if session.head_run_id is None:
            session.head_run_id = run_id
        await self._session.flush()

        await self.append_events(
            AppendEventsCommand(
                workspace_id=command.workspace_id,
                run_id=run_id,
                events=(
                    ReservedEvent(
                        RunEventType.RUN_CREATED,
                        {"session_sequence": run.session_sequence},
                    ),
                ),
            )
        )
        self._audit(
            command.workspace_id,
            command.caller.caller_id,
            "run.created",
            "run",
            run_id,
            command.request_id,
            actor_type=command.caller.caller_type.value,
        )

        snapshot = await self._snapshot(run, command.capabilities)
        document = snapshot.document()
        await self._store_response(
            command.workspace_id,
            command.caller.caller_type,
            command.caller.caller_id,
            command.endpoint,
            command.idempotency_key,
            run_id,
            document,
        )
        return AcceptedRun(run_id=run_id, document=document, replayed=False)

    async def get_run(
        self, workspace_id: UUID, run_id: UUID, capabilities: RunCapabilities
    ) -> RunSnapshot | None:
        row = await self._session.scalar(
            select(RunRow).where(RunRow.id == run_id, RunRow.workspace_id == workspace_id)
        )
        return None if row is None else await self._snapshot(row, capabilities)

    async def list_runs(
        self, workspace_id: UUID, session_id: UUID | None, capabilities: RunCapabilities
    ) -> Sequence[RunSnapshot]:
        statement = select(RunRow).where(RunRow.workspace_id == workspace_id)
        if session_id is not None:
            statement = statement.where(RunRow.session_id == session_id)
        # Two different questions, and they want opposite orders.
        #
        # Filtered to one Session, this list **is** the queue: `queue.position`
        # counts 1, 2, 3 down it, and reversing that would put position 1 at
        # the bottom. That is a transcript, where the order carries the
        # meaning.
        #
        # Unfiltered, it is a worklist — "what has been happening" — and it is
        # read from the top. Oldest-first pushed the newest Run further down
        # with every submission, so somebody who opened the console to look at
        # the thing that just happened found it last. That was asked about
        # twice: once for workspaces, and again here.
        #
        # `id` last either way. `created_at` is not a total order — Runs
        # submitted together share it to the microsecond — and without the
        # tiebreaker the database may order them differently on each request,
        # which reads as a list that shuffles itself while you look at it.
        order = (
            (RunRow.created_at, RunRow.session_sequence, RunRow.id)
            if session_id is not None
            else (
                RunRow.created_at.desc(),
                RunRow.session_sequence.desc(),
                RunRow.id.desc(),
            )
        )
        rows = (await self._session.scalars(statement.order_by(*order))).all()
        return [await self._snapshot(row, capabilities) for row in rows]

    async def usage_summary(self, workspace_id: UUID) -> WorkspaceUsageSummary:
        """§6's usage half: a workspace's spend, grouped by `cost_quality`.

        Joined on `RunBudgetScopeRow.root_run_id == RunRow.id` rather than
        `RunRow.budget_root_run_id`: a delegated child carries its parent's
        `budget_root_run_id` (§13) but owns no `run_budget_scopes` row of its
        own — only the root does. Matching the scope's own primary key
        against the *matching* Run's id, instead of every Run's
        `budget_root_run_id`, is what keeps each Run-tree's consumption
        counted exactly once rather than once per child that shares it.
        """
        statement = (
            select(
                RunBudgetScopeRow.cost_quality,
                func.sum(RunBudgetScopeRow.consumed_cost),
                func.max(RunBudgetScopeRow.cost_currency),
                func.count(),
                func.sum(RunBudgetScopeRow.consumed_model_calls),
                func.sum(RunBudgetScopeRow.consumed_tool_calls),
                func.sum(RunBudgetScopeRow.consumed_tokens),
                func.sum(RunBudgetScopeRow.consumed_execution_ms),
            )
            .select_from(RunBudgetScopeRow)
            .join(RunRow, RunRow.id == RunBudgetScopeRow.root_run_id)
            .where(RunRow.workspace_id == workspace_id)
            .group_by(RunBudgetScopeRow.cost_quality)
            .order_by(RunBudgetScopeRow.cost_quality)
        )
        rows = (await self._session.execute(statement)).all()
        by_quality = tuple(
            WorkspaceUsageByQuality(
                cost_quality=quality,
                consumed_cost=cost,
                cost_currency=currency,
                run_count=int(count),
                consumed_model_calls=int(model_calls or 0),
                consumed_tool_calls=int(tool_calls or 0),
                consumed_tokens=int(tokens or 0),
                consumed_execution_ms=int(execution_ms or 0),
            )
            for (
                quality,
                cost,
                currency,
                count,
                model_calls,
                tool_calls,
                tokens,
                execution_ms,
            ) in rows
        )
        return WorkspaceUsageSummary(
            window=USAGE_WINDOW,
            by_cost_quality=by_quality,
            total_run_count=sum(item.run_count for item in by_quality),
            total_model_calls=sum(item.consumed_model_calls for item in by_quality),
            total_tool_calls=sum(item.consumed_tool_calls for item in by_quality),
            total_tokens=sum(item.consumed_tokens for item in by_quality),
            total_execution_ms=sum(item.consumed_execution_ms for item in by_quality),
        )

    async def append_events(self, command: AppendEventsCommand) -> tuple[RunEvent, ...]:
        if not command.events:
            return ()
        await self._session.flush()
        first = await self._session.scalar(
            RESERVE_SEQUENCES,
            {
                "count": len(command.events),
                "run_id": command.run_id,
                "workspace_id": command.workspace_id,
            },
        )
        if first is None:
            raise UnknownSession
        occurred_at = datetime.now(UTC)
        written: list[RunEvent] = []
        for offset, event in enumerate(command.events):
            row = RunEventRow(
                id=uuid4(),
                run_id=command.run_id,
                workspace_id=command.workspace_id,
                sequence=first + offset,
                event_type=event.event_type.value,
                payload=event.payload,
                occurred_at=occurred_at,
            )
            self._session.add(row)
            written.append(
                RunEvent(row.id, command.run_id, row.sequence, event.event_type, occurred_at)
            )
        try:
            await self._session.flush()
        except IntegrityError as error:
            # The allocator hands out disjoint ranges, so a duplicate sequence
            # means something outside this seam wrote events.
            raise EventSequenceConflict from error
        await self._forget_cached_sequence(command.run_id)
        return tuple(written)

    async def event_window(
        self, workspace_id: UUID, run_id: UUID
    ) -> RunEventWindow | None:
        run = await self._session.scalar(
            select(RunRow).where(RunRow.id == run_id, RunRow.workspace_id == workspace_id)
        )
        if run is None:
            return None
        earliest = await self._session.scalar(
            select(func.min(RunEventRow.sequence)).where(
                RunEventRow.run_id == run_id, RunEventRow.workspace_id == workspace_id
            )
        )
        return RunEventWindow(
            earliest_sequence=None if earliest is None else int(earliest),
            next_sequence=run.next_event_sequence,
            is_terminal=RunState(run.status) in TERMINAL_STATES,
        )

    async def list_events_after(
        self, workspace_id: UUID, run_id: UUID, after_sequence: int, limit: int
    ) -> Sequence[RunEventRecord]:
        rows = (
            await self._session.scalars(
                select(RunEventRow)
                .where(
                    RunEventRow.run_id == run_id,
                    RunEventRow.workspace_id == workspace_id,
                    RunEventRow.sequence > after_sequence,
                )
                .order_by(RunEventRow.sequence)
                .limit(limit)
            )
        ).all()
        return [
            RunEventRecord(
                sequence=row.sequence,
                event_type=RunEventType(row.event_type),
                occurred_at=row.occurred_at,
                payload=row.payload,
            )
            for row in rows
        ]

    async def control_run(self, command: ControlRunCommand) -> RunSnapshot:
        """A user control request; an illegal one is audited before it fails."""
        if not command.capabilities.can_control:
            raise ForbiddenRunAction
        try:
            return await self.apply_signal(
                ApplySignalCommand(
                    workspace_id=command.workspace_id,
                    run_id=command.run_id,
                    signal=command.signal,
                    request_id=command.request_id,
                    capabilities=command.capabilities,
                    expected_state_version=command.expected_state_version,
                    payload={"requested_by": str(command.caller.caller_id)},
                )
            )
        except RunStateError as error:
            self._audit(
                command.workspace_id,
                command.caller.caller_id,
                "run.control_denied",
                "run",
                command.run_id,
                command.request_id,
                result="denied",
                context={"signal": command.signal.value},
                actor_type=command.caller.caller_type.value,
            )
            raise DeniedRunControl(_denial_code(error)) from error

    async def widen_budget(self, command: WidenBudgetCommand) -> RunSnapshot:
        """Raise one ceiling on the shared scope, leaving every counter alone.

        Not routed through `RunStateMachine`: this changes no Run state. What
        it changes is whether the machine's own `budget_allows_execution` is
        true, so a `paused(limit)` Run starts offering `resume` again without
        anything here deciding that it should.
        """
        if not command.capabilities.can_control:
            raise ForbiddenRunAction
        run = await self._lock_run(command.workspace_id, command.run_id)
        if run is None:
            raise UnknownRun
        if run.state_version != command.expected_state_version:
            raise StateVersionConflict
        budget = await self._session.scalar(
            select(RunBudgetScopeRow)
            .where(RunBudgetScopeRow.root_run_id == run.budget_root_run_id)
            .with_for_update()
        )
        if budget is None:
            raise UnknownRun
        if command.max_model_calls <= budget.max_model_calls:
            self._audit(
                command.workspace_id,
                command.caller.caller_id,
                "run.budget_widen_denied",
                "run",
                command.run_id,
                command.request_id,
                result="denied",
                context={
                    "asked": command.max_model_calls,
                    "in_force": budget.max_model_calls,
                },
                actor_type=command.caller.caller_type.value,
            )
            raise BudgetNotWidened
        previous = budget.max_model_calls
        budget.max_model_calls = command.max_model_calls
        budget.version += 1
        self._audit(
            command.workspace_id,
            command.caller.caller_id,
            "run.budget_widened",
            "run",
            command.run_id,
            command.request_id,
            context={
                "max_model_calls": {"from": previous, "to": command.max_model_calls},
                # The counters are recorded, not changed. An auditor reading
                # this row can see how much of the old ceiling was already
                # spent at the moment the new one was granted.
                "consumed_model_calls": budget.consumed_model_calls,
            },
            actor_type=command.caller.caller_type.value,
        )
        await self._session.flush()
        return await self._snapshot(run, command.capabilities)

    async def apply_signal(self, command: ApplySignalCommand) -> RunSnapshot:
        """Apply exactly the mutation ``RunStateMachine`` returns, once."""
        run = await self._lock_run(command.workspace_id, command.run_id)
        if run is None:
            raise UnknownRun
        if (
            command.expected_state_version is not None
            and run.state_version != command.expected_state_version
        ):
            raise StateVersionConflict
        session = await self._lock_session(command.workspace_id, run.session_id)
        if session is None:
            raise UnknownSession

        if command.signal is RunSignal.LIMIT_CLEANUP_CONFIRMED:
            # Design §6.3: the door opens only for the Run whose rollback
            # recorded this exact destination and this exact sandbox. The
            # Scheduler is the only caller that can name both.
            if (
                run.workspace_cleanup_target != WorkspaceCleanupTarget.PAUSED_LIMIT.value
                or run.workspace_cleanup_sandbox_id is None
                or run.workspace_cleanup_sandbox_id != command.confirmed_sandbox_id
            ):
                raise InvalidStateMetadata(
                    "the run's recorded cleanup intent does not match this confirmation"
                )
        if (
            command.signal is RunSignal.RECOVERY_FAILED
            and command.confirmed_sandbox_id is not None
        ):
            # The conflict path's confirmation: same shape, different target.
            if (
                run.workspace_cleanup_target != WorkspaceCleanupTarget.FAILED_CONFLICT.value
                or run.workspace_cleanup_sandbox_id != command.confirmed_sandbox_id
            ):
                raise InvalidStateMetadata(
                    "the run's recorded cleanup intent does not match this confirmation"
                )

        await self._decide_and_write(
            run,
            session,
            command.signal,
            pause_reason=command.pause_reason,
            wait_kind=command.wait_kind,
            wait_deadline_at=command.wait_deadline_at,
            request_id=command.request_id,
            payload=dict(command.payload),
        )
        if command.signal is RunSignal.LIMIT_CLEANUP_CONFIRMED or (
            command.signal is RunSignal.RECOVERY_FAILED
            and command.confirmed_sandbox_id is not None
        ):
            # Cleared only in the same transition that reaches the target.
            run.workspace_cleanup_target = None
            run.workspace_cleanup_sandbox_id = None
        # record_slice releases the lease when it writes a signal. The
        # committed-checkpoint path applies the real signal here after a
        # signal=None commit, and must do the same: a queued Run whose
        # WorkerLease is still held cannot be claimed until expiry, so the
        # warm thaw waits out the remaining lease instead of 300ms.
        if RunState(run.status) is not RunState.RUNNING:
            await self._release_lease(run.id)
        return await self._snapshot(run, command.capabilities)

    async def _decide_and_write(
        self,
        run: RunRow,
        session: SessionRow,
        signal: RunSignal,
        *,
        pause_reason: PauseReason | None,
        wait_kind: str | None,
        wait_deadline_at: datetime | None,
        request_id: str,
        payload: dict[str, Any],
        extra_events: tuple[ReservedEvent, ...] = (),
        wait_policy: WaitPolicy | None = None,
    ) -> None:
        """Turn one signal into rows.

        This is the only place a ``StateDecision`` becomes a database write.
        Both ``apply_signal`` and ``record_slice`` route through it so a second
        interpretation of the state machine cannot appear.
        """
        budget = await self._session.get(RunBudgetScopeRow, run.budget_root_run_id)
        if budget is None:
            raise UnknownRun
        now = datetime.now(UTC)
        decision = self._machine.decide(
            RunStateView(
                state=RunState(run.status),
                pause_reason=None if run.pause_reason is None else PauseReason(run.pause_reason),
                wait_kind=run.wait_kind,
                wait_deadline_at=run.wait_deadline_at,
                pause_requested=run.pause_requested_at is not None,
                cancel_requested=run.cancel_requested_at is not None,
                budget_allows_execution=_budget_summary(budget).allows_execution(now),
            ),
            signal,
            pause_reason=pause_reason,
            wait_kind=wait_kind,
            wait_deadline_at=wait_deadline_at,
        )

        _apply_decision(run, decision, now)
        # Cleared alongside `wait_kind` rather than left behind: a Run that is
        # no longer waiting on children must not read as one that is waiting
        # for `all` of nothing.
        run.wait_policy = (
            None if decision.wait_kind is None or wait_policy is None else wait_policy.value
        )
        run.state_version += 1
        run.updated_at = now
        await self._session.flush()

        if decision.is_terminal:
            await self._terminalize(run, session, now)

        await self.append_events(
            AppendEventsCommand(
                workspace_id=run.workspace_id,
                run_id=run.id,
                events=(ReservedEvent(event_type_for(signal), payload), *extra_events),
            )
        )
        self._audit(
            run.workspace_id,
            None,
            f"run.{signal.value}",
            "run",
            run.id,
            request_id,
        )

    async def execution_context(
        self, workspace_id: UUID, run_id: UUID
    ) -> ExecutionContext | None:
        """Read everything one round needs, and nothing a Worker may write."""
        run = await self._session.scalar(
            select(RunRow).where(RunRow.id == run_id, RunRow.workspace_id == workspace_id)
        )
        if run is None:
            return None
        version = await self._session.get(AgentVersionRow, run.agent_version_id)
        budget = await self._session.get(RunBudgetScopeRow, run.budget_root_run_id)
        if version is None or budget is None:
            return None
        # A persistent Session hands over everything said in it so far; an
        # ephemeral one hands over only this Run's own turns. That is the first
        # behaviour `session_mode` has ever had — it has been stored since phase
        # 2A and read by nothing, because a stand-in model that branches on a
        # round counter never needed a conversation.
        owning = await self._session.get(SessionRow, run.session_id)
        scoped = select(SessionMessageRow).where(
            SessionMessageRow.session_id == run.session_id,
            SessionMessageRow.redacted.is_(False),
            SessionMessageRow.withdrawn_at.is_(None),
        )
        if owning is None or owning.session_mode == SessionMode.EPHEMERAL.value:
            scoped = scoped.where(SessionMessageRow.source_run_id == run.id)
        found = await self._session.scalars(scoped.order_by(SessionMessageRow.sequence))
        spec = AgentSpec.model_validate(version.spec)
        history = tuple(
            StoredMessage(id=row.id, sequence=row.sequence, message=_to_message(row))
            for row in found
        )
        deadline = None
        if run.delivery_mode == DeliveryMode.CHAT_COMPLETIONS.value:
            deadline = run.created_at + timedelta(seconds=spec.delivery.sync_timeout_seconds)
        return ExecutionContext(
            run_id=run.id,
            state_version=run.state_version,
            spec=spec,
            history=history,
            cancel_requested=run.cancel_requested_at is not None,
            pause_requested=run.pause_requested_at is not None,
            budget=_budget_summary(budget),
            window=await self._context_window(spec),
            compat_deadline_at=deadline,
            skills=await self._bound_skills(spec),
            loaded_skills=await self._loaded_skills(run.id),
            http_operations=await self._bound_operations(spec),
            prices=await self._pinned_prices(run.model_pricing_version_id),
            memories=await self._remembered(run, owning, _latest_request(history)),
            depth=run.depth,
            delegated_scope=(
                None
                if run.delegation_scope is None
                else DelegationScope.from_document(run.delegation_scope)
            ),
            # Read off the same Session `_remembered` already reads for the
            # memory subject, and for the same reason: who may confirm a
            # write is who started the conversation, not `runs.end_user_id`
            # (see `_remembered`'s own docstring for why those are kept
            # apart).
            caller_type=None if owning is None else CallerType(owning.caller_type),
        )

    async def unfinished_work(self, session_id: UUID) -> UnfinishedWork | None:
        """这个 Session 现在有没有未了结的工作，有的话是哪一种。

        判据是「有没有非终态的 Run」，不是 `head_run_id` 是否为空——排在队首
        之后、还没被提上来的 Run 同样是未了结的工作，而 `head_run_id` 对它们
        一无所知。

        非终态里再分一刀，分的是**队首自己**的状态：停在 `WAITING_HEAD_STATES`
        上时报 `parked` 并把它的 id 和版本带出去，因为只有这一种可以被取消
        （理由见 `StoppedRun`）。这一刀故意不看队首后面还排着什么——阻塞卡片
        正是在「队首停着、你的消息排在后面」时渲染的，而卡片承诺的出口就是
        `/new`；把排队的算进来会让那句承诺在它唯一要兑现的场景里失效。

        `cancellable` 带出的是这个 Session **全部**未了结的 Run，不只是队首：
        只取消队首，后面排队的会被提上来，拿着已经被撤掉的历史跑完一整轮再答复。
        顺序是载荷的一部分——排队的在前，停住的队首在最后。队首终态时
        `_terminalize` 会把下一个提上来，先取消队首等于在中途改变自己正要读的
        那张表；今天它并不动 `state_version`，所以先后其实都能过，但依赖这一点
        等于依赖 `_terminalize` 的一个本函数没有理由知道的细节。

        **全有或全无**：只要有一个非终态的 Run 收不下 `CANCEL_REQUESTED`
        （`running`、`cancelling`），就一个都不给，报 `running`。这是事前检查，
        不是回滚——这一层没有 savepoint，而撤一半比拒绝更糟，所以挡在动手之前。

        先锁 Session 行：`accept_run`（`submit_run`/`submit_end_user_run` 都
        经它）通过 `_lock_session` 锁的是同一行（`SessionRow.id == session_id`），
        所以这里的 `with_for_update()` 与一次并发的 Run 提交确实会串行——不是
        因为它们共享同一份连接，而是因为它们请求了同一把行锁。串行化到此为
        止：它不保证撤回发生在提交「之前」或「之后」的哪一侧，只保证两者不会
        在同一时刻各自读到对方尚未写完的一半。
        """
        await self._session.execute(
            select(SessionRow.id).where(SessionRow.id == session_id).with_for_update()
        )
        rows = (
            await self._session.execute(
                select(
                    RunRow.id,
                    RunRow.status,
                    RunRow.state_version,
                    RunRow.session_sequence,
                ).where(
                    RunRow.session_id == session_id,
                    RunRow.status.not_in(tuple(s.value for s in TERMINAL_STATES)),
                )
            )
        ).all()
        if not rows:
            return None
        owning = await self._session.get(SessionRow, session_id)
        head = None if owning is None else owning.head_run_id
        at_head = next((row for row in rows if row.id == head), None)
        if at_head is None:
            # 队首自己已经终态，未了结的全排在它后面——§6 点名的那一半，
            # `head_run_id` 对它们一无所知。
            return UnfinishedWork(reason="queued")
        if at_head.status not in _PARKED_STATUSES:
            return UnfinishedWork(reason="running")
        if any(row.status not in _CANCELLABLE_STATUSES for row in rows):
            # 队首停着，但后面站着一个取消不掉的。`running` 是这里最诚实的词：
            # 它就是那个 Run 的状态。
            return UnfinishedWork(reason="running")
        behind = sorted(
            (row for row in rows if row.id != head),
            key=lambda row: row.session_sequence,
        )
        return UnfinishedWork(
            reason="parked",
            cancellable=tuple(
                StoppedRun(run_id=row.id, state_version=row.state_version)
                for row in (*behind, at_head)
            ),
        )

    async def has_waiting_run(self, session_id: UUID, after_sequence: int) -> bool:
        """是否有人排在这个 Run 后面。

        §12.1 的让位规则要的就是这一个事实。参照系是 `session_sequence` 而不是
        `head_run_id`：让位是为了让**后面**那条消息跑起来，而「谁是队首」在这一刻
        必然是提问者自己，问它得不到答案。

        `EXISTS` 而不是取回行：调用方只需要真假，而一个 Session 后面可能排着很多条。
        """
        return bool(
            await self._session.scalar(
                select(
                    exists().where(
                        RunRow.session_id == session_id,
                        RunRow.session_sequence > after_sequence,
                        RunRow.status.not_in(tuple(s.value for s in TERMINAL_STATES)),
                    )
                )
            )
        )

    async def withdrawable(
        self, session_id: UUID, scope: WithdrawScope, turns: int
    ) -> tuple[list[UUID], int, str]:
        """要撤的行、实际轮数、被撤那条 user 消息的原文。

        `LAST_EXCHANGE` 的锚点必须是 user 消息（上游同样如此）：从一条 assistant
        消息往回撤，撤出来的是半轮，重发时对话会错位。
        """
        found = (
            await self._session.scalars(
                select(SessionMessageRow)
                .where(
                    SessionMessageRow.session_id == session_id,
                    SessionMessageRow.redacted.is_(False),
                    SessionMessageRow.withdrawn_at.is_(None),
                )
                .order_by(SessionMessageRow.sequence)
            )
        ).all()
        if not found:
            return [], 0, ""
        if scope is WithdrawScope.ALL:
            anchors = [row for row in found if row.role == "user"]
            text = _text_of(anchors[0]) if anchors else ""
            return [row.id for row in found], len(anchors), text
        users = [row for row in found if row.role == "user"]
        if not users:
            return [], 0, ""
        index = max(len(users) - turns, 0)
        anchor = users[index]
        taken = [row for row in found if row.sequence >= anchor.sequence]
        return [row.id for row in taken], len(users) - index, _text_of(anchor)

    async def mark_withdrawn(self, message_ids: Sequence[UUID], *, at: datetime) -> int:
        """置时间戳，且只置一次；顺带作废覆盖到它们的那份压缩摘要。

        `withdrawn_at.is_(None)` 不是防御性的多余条件：撤回是幂等的，重放同一条
        命令不得把第一次撤回的时刻改写成第二次的。

        摘要那一步和撤回同一个事务，不是另开一个方法让调用方记得调：一份提炼过
        被撤内容的摘要，下一轮会被 `_plan_context` 原样发回模型（复用判据只看
        `last_sequence` 够不够远），撤回就漏在这条路上。作废之后下一次压缩重新
        生成一份——不去改摘要正文，那是拿模型写的话猜它哪几句该删。
        """
        if not message_ids:
            return 0
        # `RETURNING id` rather than `rowcount`, for the same reason
        # `forget_deliveries_before` gives: a `Result`'s `rowcount` is not
        # typed as available on every dialect, while the ids the update
        # actually named are both checkable and dialect-independent.
        updated = await self._session.scalars(
            update(SessionMessageRow)
            .where(
                SessionMessageRow.id.in_(message_ids),
                SessionMessageRow.withdrawn_at.is_(None),
            )
            .values(withdrawn_at=at)
            .returning(SessionMessageRow.id)
        )
        withdrawn = list(updated.all())
        await self._forget_summaries_covering(withdrawn)
        await self._session.flush()
        return len(withdrawn)

    async def _forget_summaries_covering(self, message_ids: Sequence[UUID]) -> None:
        """删掉正文范围里含有这些消息的摘要行。

        判据是 `first_sequence <= sequence <= last_sequence`——摘要覆盖的是一个
        序号区间，只比 `last_sequence` 会把区间之前的历史也算进去。这是
        `StoredSummary.first_sequence` 的读者。

        只看这次真正落笔的那几行（`mark_withdrawn` 的 RETURNING）：已经撤过的
        消息，当时就作废过一次，之后重新生成的摘要是从不含它们的历史里写出来
        的，不该被同一条命令的重放再扔一次。
        """
        if not message_ids:
            return
        covered = (
            select(SessionMessageRow.id)
            .where(
                SessionMessageRow.id.in_(message_ids),
                SessionMessageRow.session_id == SessionCompactionRow.session_id,
                SessionMessageRow.sequence >= SessionCompactionRow.first_sequence,
                SessionMessageRow.sequence <= SessionCompactionRow.last_sequence,
            )
            .exists()
        )
        await self._session.execute(delete(SessionCompactionRow).where(covered))

    async def withdrawn_at_of(self, message_id: UUID) -> datetime | None:
        row = await self._session.get(SessionMessageRow, message_id)
        return None if row is None else row.withdrawn_at

    async def latest_summary(self, session_id: UUID) -> StoredSummary | None:
        row = await self._session.scalar(
            select(SessionCompactionRow).where(
                SessionCompactionRow.session_id == session_id
            )
        )
        if row is None:
            return None
        return StoredSummary(
            session_id=row.session_id,
            first_sequence=row.first_sequence,
            last_sequence=row.last_sequence,
            text=row.summary,
            endpoint_id=row.endpoint_id,
            model=row.model,
        )

    async def save_summary(
        self, summary: StoredSummary, *, workspace_id: UUID
    ) -> None:
        """Upsert on `session_id`, per `uq_session_compactions_session` — the
        constraint that makes "only the latest is kept" true rather than a
        claim this method merely intends.
        """
        await self._session.execute(
            pg_insert(SessionCompactionRow)
            .values(
                id=uuid4(),
                session_id=summary.session_id,
                workspace_id=workspace_id,
                first_sequence=summary.first_sequence,
                last_sequence=summary.last_sequence,
                summary=summary.text,
                endpoint_id=summary.endpoint_id,
                model=summary.model,
            )
            .on_conflict_do_update(
                constraint="uq_session_compactions_session",
                set_={
                    "first_sequence": summary.first_sequence,
                    "last_sequence": summary.last_sequence,
                    "summary": summary.text,
                    "endpoint_id": summary.endpoint_id,
                    "model": summary.model,
                },
            )
        )
        await self._session.flush()

    async def record_summary_usage(self, command: RecordSummaryUsageCommand) -> None:
        """Bill a summarization call, and append its event, in one
        transaction — see `RecordSummaryUsageCommand`.

        Not `_consume_budget`: that also moves `consumed_execution_ms`, which
        a summarization call must never touch — `executed_ms` is never even
        measured for it. `consumed_model_calls` moves here
        instead, deliberately (§12.4, product decision): the call counter,
        the token counter and the cost counter are one valve honoured
        together, not two of three, and the call counter is the one that
        still functions on a deployment with no price, or no `max_cost`,
        configured — the default shape, where the other two can say nothing
        at all.
        """
        consumed = await self._session.scalar(
            update(RunBudgetScopeRow)
            .where(RunBudgetScopeRow.root_run_id == command.root_run_id)
            .values(
                consumed_model_calls=(
                    RunBudgetScopeRow.consumed_model_calls + command.model_calls
                ),
                consumed_tokens=RunBudgetScopeRow.consumed_tokens + command.tokens,
                version=RunBudgetScopeRow.version + 1,
            )
            .returning(RunBudgetScopeRow.version)
            .execution_options(synchronize_session=False)
        )
        if consumed is None:
            raise UnknownRun
        budget = await self._session.get(RunBudgetScopeRow, command.root_run_id)
        if budget is not None:
            _accumulate_cost(budget, command.cost)
            # Written before the refresh below for the same reason
            # `_consume_budget` orders its own two calls this way: the refresh
            # re-reads the row, and without a flush first it would discard
            # what `_accumulate_cost` just set in memory.
            await self._session.flush()
            await self._session.refresh(budget)
        await self.append_events(
            AppendEventsCommand(
                workspace_id=command.workspace_id,
                run_id=command.run_id,
                events=(command.event,),
            )
        )

    async def current_prices_for(self, endpoint_id: UUID) -> TokenPrices | None:
        """The price in force for this endpoint right now.

        For a genuinely different, declared summary endpoint only
        (§7.4.2 Task 4's `summary_endpoint_id`) — **never** for a Run's own
        main endpoint. That price is pinned at Run creation
        (`runs.model_pricing_version_id`, read back as `context.prices`) so a
        later repricing cannot change what an already-running Run is charged;
        calling this instead for the main endpoint would read today's price
        and quietly break that pin. A declared summary endpoint has no
        equivalent pin — `model_pricing_version_id` is a single column, and it
        already names the main endpoint's version — so the price in force at
        call time is the only one there is for it. Same query
        `_current_pricing` runs, just keyed directly on an endpoint rather
        than resolved through a Version's spec. `Worker._bill_summary_call` is
        the caller that actually holds the "which endpoint is this" decision
        and chooses between this and `context.prices`; this method has no way
        to enforce that choice itself.
        """
        row = await self._session.scalar(
            select(ModelPricingVersionRow)
            .where(
                ModelPricingVersionRow.endpoint_id == endpoint_id,
                ModelPricingVersionRow.effective_at <= datetime.now(UTC),
            )
            .order_by(
                ModelPricingVersionRow.effective_at.desc(),
                ModelPricingVersionRow.version_number.desc(),
            )
            .limit(1)
        )
        if row is None:
            return None
        return TokenPrices(
            currency=row.currency,
            input_per_million=row.input_per_million,
            output_per_million=row.output_per_million,
            cached_input_per_million=row.cached_input_per_million,
        )

    async def _bound_skills(self, spec: AgentSpec) -> tuple[BoundSkill, ...]:
        """What the Version bound, in the order the author bound it.

        Read by version id and nothing else. The skill's own name column
        answers what the model calls it, and the version's manifest answers
        what it is for — read from the version rather than from the skill,
        because a later version may describe itself differently and this Run is
        not on it.

        A binding whose rows are gone is skipped rather than failing the round.
        Publishing checked every one of them, so this can only happen after a
        deletion, and a Run that cannot start because one of four skills was
        deleted is worse off than one running with three.
        """
        if not spec.skills:
            return ()
        wanted = [binding.skill_version_id for binding in spec.skills]
        found = await self._session.execute(
            select(SkillVersionRow.id, SkillVersionRow.manifest, SkillRow.name)
            .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
            .where(SkillVersionRow.id.in_(wanted))
        )
        rows = {
            version_id: (manifest, name) for version_id, manifest, name in found.all()
        }
        skills: list[BoundSkill] = []
        for version_id in wanted:
            row = rows.get(version_id)
            if row is None:
                continue
            manifest, name = row
            skills.append(
                BoundSkill(
                    skill_version_id=version_id,
                    name=name,
                    description=str(manifest.get("description", "")),
                )
            )
        return tuple(skills)

    async def _remembered(
        self, run: RunRow, session: SessionRow | None, query: str
    ) -> tuple[RememberedFact, ...]:
        """This Run's own two scopes, read as two scoped queries.

        The subject comes from the Session's `CallerIdentity`, which is who
        started the conversation — not from `runs.end_user_id`, which is who
        may confirm. They are the same person for a `caller_type=user` Run
        today, and writing the difference down here is what will keep §4.5's
        end-user identity from being wired to the wrong one.

        `query` is this Run's latest request, and the ordering it produces is
        **keyword relevance, not meaning** (§14.3 excludes vector memory). It
        decides which memories the planner sees first, and therefore which ones
        survive when the segment is over budget.

        A Session that is gone means no subject, and no subject means no
        private memory rather than everybody's.
        """
        if session is None:  # pragma: no cover - the caller read it
            return ()
        agent_id = await self._session.scalar(
            select(AgentVersionRow.agent_id).where(
                AgentVersionRow.id == run.agent_version_id
            )
        )
        if agent_id is None:  # pragma: no cover - a Run always has a version
            return ()
        library = SqlMemoryLibrary(self._session)
        found: list[RememberedFact] = []
        for scope in scopes_for_run(
            workspace_id=run.workspace_id,
            agent_id=agent_id,
            subject=CallerIdentity(
                caller_type=CallerType(session.caller_type),
                caller_id=session.caller_id,
            ),
        ):
            found.extend(
                await library.relevant_in(scope, query, limit=MEMORY_READ_LIMIT)
            )
        return tuple(found)

    async def _pinned_prices(self, version_id: UUID | None) -> TokenPrices | None:
        """The price this Run fixed, read back.

        Read from the version rather than from the endpoint's current price:
        that is the whole of §12.4's promise, and reading the current one here
        would quietly undo it every time an administrator corrected a rate.
        """
        if version_id is None:
            return None
        row = await self._session.get(ModelPricingVersionRow, version_id)
        if row is None:
            return None
        return TokenPrices(
            currency=row.currency,
            input_per_million=row.input_per_million,
            output_per_million=row.output_per_million,
            cached_input_per_million=row.cached_input_per_million,
        )

    async def _bound_operations(self, spec: AgentSpec) -> tuple[BoundOperation, ...]:
        """What the Version bound, assembled into callable operations.

        Read by version id, like the skills. The tool row answers where the
        requests go and which credential to resolve; the version row answers
        what the operation is. Both are read here rather than at the call so a
        round composes its request from one consistent read.

        An operation the version no longer declares is skipped rather than
        failing the round — publishing checked every one, so this can only
        follow a deletion, and a Run that loses one of four tools is better off
        than one that cannot start.
        """
        if not spec.http_tools:
            return ()
        wanted = [binding.http_tool_version_id for binding in spec.http_tools]
        found = await self._session.execute(
            select(
                HttpToolVersionRow.id,
                HttpToolVersionRow.operations,
                HttpToolRow.name,
                HttpToolRow.base_url,
                HttpToolRow.credential_ref,
            )
            .join(HttpToolRow, HttpToolRow.id == HttpToolVersionRow.http_tool_id)
            .where(HttpToolVersionRow.id.in_(wanted))
        )
        rows = {row[0]: row for row in found.all()}
        operations: list[BoundOperation] = []
        for binding in spec.http_tools:
            row = rows.get(binding.http_tool_version_id)
            if row is None:
                continue
            _, documents, name, base_url, credential_ref = row
            declared = {
                str(entry["operation_id"]): entry
                for entry in cast(list[dict[str, Any]], documents)
            }
            for operation_id in binding.operations:
                document = declared.get(operation_id)
                if document is None:
                    continue
                operations.append(
                    BoundOperation(
                        tool_name=name,
                        version_id=binding.http_tool_version_id,
                        base_url=base_url,
                        credential_ref=credential_ref,
                        operation=operation_from_document(document),
                    )
                )
        return tuple(operations)

    async def _loaded_skills(self, run_id: UUID) -> tuple[UUID, ...]:
        """Which versions this Run has already read text from, oldest first."""
        found = await self._session.scalars(
            select(RunEventRow.payload)
            .where(
                RunEventRow.run_id == run_id,
                RunEventRow.event_type == RunEventType.SKILL_LOADED.value,
            )
            .order_by(RunEventRow.sequence)
        )
        loaded: list[UUID] = []
        for payload in found:
            raw = payload.get("skill_version_id")
            if isinstance(raw, str):
                loaded.append(UUID(raw))
        return tuple(loaded)

    async def _context_window(self, spec: AgentSpec) -> ContextWindow | None:
        """What the endpoint this Run's policy names declared it can take.

        Read from the endpoint row rather than guessed from the provider name,
        per §7.4.2 — and read fresh each round rather than frozen into the
        Version, because the window is a property of the endpoint an
        administrator maintains, not of the Agent an author published.
        """
        policy = spec.model_policy
        if not isinstance(policy, EndpointModelPolicy):
            # The deterministic model declares no window. Nothing to plan
            # against, and inventing one would trim a stand-in's conversation
            # against a number no endpoint ever gave.
            return None
        endpoint = await self._session.get(ModelEndpointRow, policy.endpoint_id)
        if endpoint is None:
            return None
        return ContextWindow(
            context_window=endpoint.context_window,
            # The Agent's own ceiling narrows the endpoint's, never widens it —
            # the same minimum `build_payload` sends, so the round is planned
            # against the space the request will actually leave.
            reserved_output_tokens=min(
                policy.max_output_tokens or endpoint.max_output_tokens,
                endpoint.max_output_tokens,
            ),
            accounting=Accounting(endpoint.context_accounting),
            tokenizer=endpoint.tokenizer,
        )

    async def renew_lease(self, command: RenewLeaseCommand) -> RenewedLease | None:
        """Extend a lease this worker still holds, or report that it is gone.

        The predicate is the concurrency control, so no row lock is needed: a
        renewal that matches nothing means the Scheduler already reclaimed the
        Run and its decision stands.
        """
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=command.lease_seconds)
        renewed = await self._session.scalar(
            update(WorkerLeaseRow)
            .where(
                WorkerLeaseRow.id == command.lease_id,
                WorkerLeaseRow.run_id == command.run_id,
                WorkerLeaseRow.version == command.expected_version,
                WorkerLeaseRow.released_at.is_(None),
            )
            .values(expires_at=expires_at, version=WorkerLeaseRow.version + 1)
            .returning(WorkerLeaseRow.version)
            .execution_options(synchronize_session=False)
        )
        if renewed is None:
            return None
        await self._session.execute(
            update(RunRow)
            .where(RunRow.id == command.run_id, RunRow.workspace_id == command.workspace_id)
            .values(last_heartbeat_at=now)
            .execution_options(synchronize_session=False)
        )
        return RenewedLease(
            lease_id=command.lease_id, version=renewed, expires_at=expires_at
        )

    async def record_slice(self, command: RecordSliceCommand) -> RunSnapshot:
        """Persist one slice's checkpoint, accounting, state change, and lease."""
        run = await self._lock_run(command.workspace_id, command.run_id)
        if run is None:
            raise UnknownRun
        lease = await self._lock_lease(command.lease_id, command.run_id)
        # Ownership is the lease id, not the renewal counter. A re-claim mints
        # a fresh id (`claim` upserts `id=uuid4()`) and a Scheduler reclaim sets
        # `released_at`, so those two already fence a Worker that lost the Run.
        # The version is bumped by the holder's *own* renewals, so comparing it
        # here rejected every write whose round outlived one renewal interval —
        # a 100 MiB checkpoint under a 20s lease, every time. See
        # docs/superpowers/verification/2026-08-15-m1-drills-16gb.md.
        if lease is None or lease.released_at is not None:
            raise LeaseLost
        if run.state_version != command.expected_state_version:
            raise StateVersionConflict
        session = await self._lock_session(command.workspace_id, run.session_id)
        if session is None:
            raise UnknownSession

        now = datetime.now(UTC)
        run.checkpoint = command.checkpoint
        run.checkpoint_replay_safe = command.checkpoint_replay_safe
        run.checkpoint_effect_status = command.checkpoint_effect_status.value
        run.updated_at = now
        if command.workspace_cleanup_target is not None:
            # Design §6.3: the destination and the sandbox to confirm, written
            # in the same transaction as the rollback results they belong to.
            run.workspace_cleanup_target = command.workspace_cleanup_target.value
            run.workspace_cleanup_sandbox_id = command.workspace_cleanup_sandbox_id
        # In this transaction and not a second one. A round whose text was
        # produced but not recorded would leave the transcript short, and the
        # next round would then build a different conversation than the one that
        # actually happened.
        for message in command.appended:
            self._session.add(
                SessionMessageRow(
                    id=uuid4(),
                    session_id=session.id,
                    workspace_id=command.workspace_id,
                    sequence=session.next_message_sequence,
                    role=message.role,
                    content=message.document(),
                    source_run_id=run.id,
                    redacted=False,
                    created_at=now,
                )
            )
            session.next_message_sequence += 1
        await self._consume_budget(
            run.budget_root_run_id,
            command.executed_ms,
            command.model_calls,
            command.tokens,
            command.cost,
        )

        if command.signal is None:
            # A round that keeps the slice changes no state, so it has no
            # transition to hang an event on — and that is exactly the Run
            # whose timeline would otherwise be empty for however many rounds
            # it worked. Its events are written on their own.
            await self.append_events(
                AppendEventsCommand(
                    workspace_id=command.workspace_id,
                    run_id=command.run_id,
                    events=command.events,
                )
            )
            await self._session.flush()
            return await self._snapshot(run, command.capabilities)

        extra = (
            (ReservedEvent(RunEventType.RUN_LIMIT_REACHED, {"reason": "budget"}),)
            if command.limit_reached
            else ()
        )
        await self._decide_and_write(
            run,
            session,
            command.signal,
            pause_reason=command.pause_reason,
            wait_kind=command.wait_kind,
            wait_policy=command.wait_policy,
            # Measured from this transaction's `now`, the same instant the
            # transition and its event are stamped with. A deadline carried in
            # from the Worker would be a few hundred milliseconds older than
            # the row announcing it, and on a slow round rather more.
            wait_deadline_at=(
                None
                if command.wait_seconds is None
                else now + timedelta(seconds=command.wait_seconds)
            ),
            request_id=command.request_id,
            payload=_slice_payload(command.executed_ms, command.checkpoint),
            extra_events=(*extra, *command.events),
        )
        lease.released_at = now
        await self._session.flush()
        return await self._snapshot(run, command.capabilities)

    async def _consume_budget(
        self,
        root_run_id: UUID,
        executed_ms: int,
        model_calls: int,
        tokens: int,
        cost: Cost | None = None,
    ) -> None:
        """Accumulate one slice's usage on the single root budget row."""
        consumed = await self._session.scalar(
            update(RunBudgetScopeRow)
            .where(RunBudgetScopeRow.root_run_id == root_run_id)
            .values(
                consumed_execution_ms=RunBudgetScopeRow.consumed_execution_ms + executed_ms,
                consumed_model_calls=RunBudgetScopeRow.consumed_model_calls + model_calls,
                consumed_tokens=RunBudgetScopeRow.consumed_tokens + tokens,
                version=RunBudgetScopeRow.version + 1,
            )
            .returning(RunBudgetScopeRow.version)
            .execution_options(synchronize_session=False)
        )
        if consumed is None:
            raise UnknownRun
        budget = await self._session.get(RunBudgetScopeRow, root_run_id)
        if budget is not None:
            _accumulate_cost(budget, cost)
            # Written before the refresh below, which re-reads the row to pick
            # up what the statement above set. Without this the refresh would
            # discard the cost that was just accumulated in memory.
            await self._session.flush()
            await self._session.refresh(budget)

    async def _lock_lease(self, lease_id: UUID, run_id: UUID) -> WorkerLeaseRow | None:
        return await self._session.scalar(
            select(WorkerLeaseRow)
            .where(WorkerLeaseRow.id == lease_id, WorkerLeaseRow.run_id == run_id)
            .with_for_update()
        )

    async def child_result_for(self, run: RunRow) -> dict[str, Any]:
        """What a child reports, and deliberately not what it did.

        §13's seventh clause: an outcome, a sentence, and the Artifacts it was
        authorized to hand over — never the conversation. The child's own
        transcript stays in the child's Session, where a person can read it and
        where the parent's context planner never has to decide whether to trim
        somebody else's turns to make room.

        The summary is the child's last words rather than a generated
        precis: it is what the child itself chose to say it had done, and a
        second model call to compress it would be a claim nobody made.

        Public rather than the private name this method carried before, purely
        as a test seam — production still reaches this only from `_terminalize`.
        """
        said = await self._session.scalar(
            select(SessionMessageRow)
            .where(
                SessionMessageRow.session_id == run.session_id,
                SessionMessageRow.role == "assistant",
                SessionMessageRow.redacted.is_(False),
                # A withdrawn message means it must not be quoted back as a
                # result — the parent would be reading words the child took
                # back, through a channel §13 never meant as a second
                # transcript.
                SessionMessageRow.withdrawn_at.is_(None),
            )
            .order_by(SessionMessageRow.sequence.desc())
            .limit(1)
        )
        summary = "" if said is None else _to_message(said).text
        produced = (
            await self._session.scalars(
                select(ArtifactRow.id)
                .where(ArtifactRow.run_id == run.id)
                .order_by(ArtifactRow.created_at, ArtifactRow.id)
            )
        ).all()
        return {
            "status": run.status,
            # Truncated with a number rather than silently: a parent reading a
            # cut-off sentence cannot tell that it is holding half of one.
            "summary": summary[:MAX_CHILD_SUMMARY],
            "summary_truncated": len(summary) > MAX_CHILD_SUMMARY,
            "failure_reason": _failure_reason(run.checkpoint),
            # What the child produced, by id. The parent is granted each of
            # them at delivery — §13's eighth clause going upward — so this
            # list is a set of things it can actually open rather than a
            # catalogue of things it may not.
            "artifacts": [str(item) for item in produced],
        }

    async def _terminalize(self, run: RunRow, session: SessionRow, now: datetime) -> None:
        """Close a Run out and hand the Session to the next eligible Run."""
        if run.parent_run_id is not None and run.delegation_result is None:
            # Written here rather than delivered here, and that is the whole of
            # §13's tenth clause: the parent is very often not in a state that
            # can take an answer — another Worker holds it, or it is still
            # waiting on a sibling — and a row survives that where a call would
            # not. The sweep hands it over when the parent can take it.
            run.delegation_result = await self.child_result_for(run)
        await self._session.execute(
            update(IdempotencyRecordRow)
            .where(IdempotencyRecordRow.run_id == run.id)
            .values(expires_at=now + IDEMPOTENCY_RETENTION)
        )
        if session.head_run_id != run.id:
            return
        successor = await self._session.scalar(
            select(RunRow)
            .where(
                RunRow.session_id == session.id,
                RunRow.id != run.id,
                RunRow.status.not_in([state.value for state in TERMINAL_STATES]),
            )
            .order_by(RunRow.session_sequence)
            .limit(1)
            .with_for_update()
        )
        session.head_run_id = None if successor is None else successor.id
        if successor is not None:
            successor.blocked_by_run_id = None
            successor.updated_at = now
        await self._session.execute(
            update(RunRow)
            .where(
                RunRow.session_id == session.id,
                RunRow.blocked_by_run_id == run.id,
                RunRow.id != (successor.id if successor is not None else run.id),
            )
            .values(blocked_by_run_id=session.head_run_id, updated_at=now)
        )
        await self._session.flush()

    async def claim_head(self, command: ClaimRunCommand) -> ClaimedRun | None:
        """Take one queued Head Run and its single lease, or take nothing.

        This slice only fixes the transaction shape; it does not renew, expire,
        or execute the lease.
        """
        now = datetime.now(UTC)
        candidate = await self._select_claimable(command, now)
        if candidate is None:
            return None

        workspace_id = candidate.workspace_id
        session = await self._lock_session(workspace_id, candidate.session_id)
        if session is None or session.head_run_id != candidate.id:
            return None

        budget = await self._session.get(RunBudgetScopeRow, candidate.budget_root_run_id)
        if budget is None:
            raise UnknownRun
        decision = self._machine.decide(
            RunStateView(
                state=RunState(candidate.status),
                budget_allows_execution=_budget_summary(budget).allows_execution(now),
            ),
            RunSignal.LEASE_ACQUIRED,
        )
        _apply_decision(candidate, decision, now)
        candidate.state_version += 1
        candidate.updated_at = now

        lease_id = uuid4()
        expires_at = now + timedelta(seconds=command.lease_seconds)
        # A re-claim reuses the one lease row per Run, so the version this
        # claimer must present is whatever the upsert produced, not always one.
        lease_version = await self._session.scalar(
            pg_insert(WorkerLeaseRow)
            .values(
                id=lease_id,
                run_id=candidate.id,
                worker_id=command.worker_id,
                acquired_at=now,
                expires_at=expires_at,
                released_at=None,
                version=1,
            )
            .on_conflict_do_update(
                constraint="uq_worker_leases_run",
                set_={
                    "id": lease_id,
                    "worker_id": command.worker_id,
                    "acquired_at": now,
                    "expires_at": expires_at,
                    "released_at": None,
                    "version": WorkerLeaseRow.version + 1,
                },
            )
            .returning(WorkerLeaseRow.version)
        )
        if lease_version is None:
            raise UnknownRun
        await self._session.flush()

        await self.append_events(
            AppendEventsCommand(
                workspace_id=workspace_id,
                run_id=candidate.id,
                events=(
                    ReservedEvent(
                        RunEventType.RUN_LEASE_ACQUIRED, {"worker_id": command.worker_id}
                    ),
                ),
            )
        )
        self._audit(
            workspace_id,
            None,
            "run.lease_acquired",
            "run",
            candidate.id,
            command.request_id,
        )
        snapshot = await self._snapshot(candidate, command.capabilities)
        return ClaimedRun(
            run=snapshot,
            lease_id=lease_id,
            lease_version=lease_version,
            lease_expires_at=expires_at,
        )

    async def _select_claimable(
        self, command: ClaimRunCommand, now: datetime
    ) -> RunRow | None:
        """Queued, Session Head, unblocked, and not already leased."""
        held = (
            select(WorkerLeaseRow.run_id)
            .where(WorkerLeaseRow.released_at.is_(None), WorkerLeaseRow.expires_at > now)
            .scalar_subquery()
        )
        statement = (
            select(RunRow)
            .join(SessionRow, SessionRow.id == RunRow.session_id)
            .where(
                RunRow.status == RunState.QUEUED.value,
                SessionRow.head_run_id == RunRow.id,
                RunRow.blocked_by_run_id.is_(None),
                RunRow.id.not_in(held),
            )
            .order_by(RunRow.created_at, RunRow.id)
            .limit(1)
            .with_for_update(of=RunRow, skip_locked=True)
        )
        if command.workspace_id is not None:
            statement = statement.where(RunRow.workspace_id == command.workspace_id)
        if command.session_id is not None:
            statement = statement.where(RunRow.session_id == command.session_id)
        return await self._session.scalar(statement)

    async def try_scan_lock(self, name: str) -> bool:
        """Take the advisory lock for one scan family, or report it is taken.

        The lock is an efficiency measure only: every repair below still uses
        row locks and version predicates, so a lost lock cannot cause a wrong
        result, only a skipped cycle.
        """
        taken = await self._session.scalar(
            select(func.pg_try_advisory_xact_lock(_scan_lock_key(name)))
        )
        return bool(taken)

    async def expired_lease_runs(self, now: datetime, limit: int) -> Sequence[UUID]:
        rows = await self._session.scalars(
            select(RunRow.id)
            .join(WorkerLeaseRow, WorkerLeaseRow.run_id == RunRow.id)
            .where(
                RunRow.status.in_(
                    [RunState.RUNNING.value, RunState.CANCELLING.value]
                ),
                WorkerLeaseRow.released_at.is_(None),
                WorkerLeaseRow.expires_at <= now,
            )
            .order_by(RunRow.updated_at)
            .limit(limit)
        )
        return list(rows.all())

    async def reclaim_expired_lease(self, run_id: UUID, request_id: str) -> None:
        """Interrupt a Run whose Worker stopped renewing.

        There is no sandbox to stop in phase 2B, so the product's
        sandbox-stopped precondition is satisfied by construction. Phase 3 adds
        the real check here rather than pretending one exists now.
        """
        run = await self._lock_run_any_workspace(run_id)
        if run is None or RunState(run.status) in TERMINAL_STATES:
            return
        session = await self._lock_session(run.workspace_id, run.session_id)
        if session is None:
            return
        await self._release_lease(run_id)
        await self._decide_and_write(
            run,
            session,
            RunSignal.INTERRUPTED,
            pause_reason=None,
            wait_kind=None,
            wait_deadline_at=None,
            request_id=request_id,
            payload={"reason": "lease_expired"},
        )

    async def recover_interrupted(
        self, run_id: UUID, max_attempts: int, request_id: str
    ) -> RunState | None:
        """Return an interrupted Run to the queue only when that is safe."""
        run = await self._lock_run_any_workspace(run_id)
        if run is None or RunState(run.status) is not RunState.INTERRUPTED:
            return None
        if run.workspace_cleanup_target is not None:
            # This Run's destination is already recorded (design §6.3); the
            # workspace cleanup job owns it, and recovery inventing a
            # different outcome here would race the recorded one.
            return None
        session = await self._lock_session(run.workspace_id, run.session_id)
        budget = await self._session.get(RunBudgetScopeRow, run.budget_root_run_id)
        if session is None or budget is None:
            return None

        effect = CheckpointEffectStatus(run.checkpoint_effect_status)
        # Design §12: a Run whose checkpoint names one revision while the
        # Session's pointer names another cannot be replayed automatically —
        # restoring would hand the model a workspace its transcript never saw.
        revision_agrees = (
            run.checkpoint_workspace_revision_id is None
            or run.checkpoint_workspace_revision_id == session.workspace_revision_id
        )
        safe = (
            run.checkpoint_replay_safe
            and revision_agrees
            and effect is not CheckpointEffectStatus.UNKNOWN
            and _budget_summary(budget).allows_execution(datetime.now(UTC))
            and run.recovery_attempts < max_attempts
        )
        if safe:
            run.recovery_attempts += 1
        signal = RunSignal.RECOVERY_APPROVED if safe else RunSignal.RECOVERY_FAILED
        await self._decide_and_write(
            run,
            session,
            signal,
            pause_reason=None,
            wait_kind=None,
            wait_deadline_at=None,
            request_id=request_id,
            payload={"replay_safe": run.checkpoint_replay_safe},
        )
        return RunState(run.status)

    async def interrupted_runs(self, limit: int) -> Sequence[UUID]:
        rows = await self._session.scalars(
            select(RunRow.id)
            .where(RunRow.status == RunState.INTERRUPTED.value)
            .order_by(RunRow.updated_at)
            .limit(limit)
        )
        return list(rows.all())

    async def workspace_of(self, run_id: UUID) -> UUID | None:
        return await self._session.scalar(
            select(RunRow.workspace_id).where(RunRow.id == run_id)
        )

    async def sessions_needing_repair(self, limit: int) -> Sequence[UUID]:
        """Sessions whose head or blockers disagree with the FIFO invariant."""
        live = (
            select(
                RunRow.session_id.label("session_id"),
                func.min(RunRow.session_sequence).label("smallest"),
            )
            .where(RunRow.status.not_in([state.value for state in TERMINAL_STATES]))
            .group_by(RunRow.session_id)
            .subquery()
        )
        head = (
            select(RunRow.session_id, RunRow.session_sequence, RunRow.status)
            .subquery()
        )
        statement = (
            select(SessionRow.id)
            .outerjoin(live, live.c.session_id == SessionRow.id)
            .outerjoin(
                head,
                (head.c.session_id == SessionRow.id)
                & (SessionRow.head_run_id.is_not(None)),
            )
            .where(
                (
                    (SessionRow.head_run_id.is_(None)) & (live.c.smallest.is_not(None))
                )
                | (
                    (SessionRow.head_run_id.is_not(None)) & (live.c.smallest.is_(None))
                )
                | SessionRow.id.in_(
                    select(RunRow.session_id).where(
                        RunRow.status.not_in(
                            [state.value for state in TERMINAL_STATES]
                        ),
                        RunRow.id != SessionRow.head_run_id,
                        RunRow.blocked_by_run_id.is_distinct_from(
                            SessionRow.head_run_id
                        ),
                    )
                )
                | SessionRow.id.in_(
                    select(RunRow.session_id).where(
                        RunRow.id == SessionRow.head_run_id,
                        RunRow.session_sequence > live.c.smallest,
                    )
                )
                | SessionRow.head_run_id.in_(
                    select(RunRow.id).where(
                        RunRow.status.in_([state.value for state in TERMINAL_STATES])
                    )
                )
            )
            .distinct()
            .limit(limit)
        )
        rows = await self._session.scalars(statement)
        return list(rows.all())

    async def expired_wait_runs(
        self, now: datetime, limit: int
    ) -> Sequence[tuple[UUID, str | None]]:
        """Runs whose deadline has passed, each with what it was waiting for.

        The kind comes back with the id because it decides what reaching the
        deadline *means*. For a timer the platform owns the deadline and
        reaching it is the wake; for a kind whose wake comes from outside —
        an approval, a child Run — reaching it means nobody answered.
        """
        rows = await self._session.execute(
            select(RunRow.id, RunRow.wait_kind)
            .where(
                RunRow.status == RunState.WAITING_EXTERNAL.value,
                RunRow.wait_deadline_at.is_not(None),
                RunRow.wait_deadline_at <= now,
            )
            .order_by(RunRow.wait_deadline_at)
            .limit(limit)
        )
        return [(row.id, row.wait_kind) for row in rows.all()]

    async def wake_external_wait(self, run_id: UUID, request_id: str) -> bool:
        """A timer that came due. Back to the queue, not into a pause."""
        run = await self._lock_run_any_workspace(run_id)
        if run is None or RunState(run.status) is not RunState.WAITING_EXTERNAL:
            return False
        session = await self._lock_session(run.workspace_id, run.session_id)
        if session is None:
            return False
        await self._decide_and_write(
            run,
            session,
            RunSignal.EXTERNAL_READY,
            pause_reason=None,
            wait_kind=None,
            wait_deadline_at=None,
            request_id=request_id,
            payload={"reason": "wait_timer_elapsed"},
        )
        return True

    async def expired_approvals(
        self, now: datetime, limit: int
    ) -> Sequence[tuple[UUID, UUID]]:
        """Pending approvals nobody answered in time, with the Run each stopped.

        Read from the approvals rather than from the Runs, because the approval
        is where the deadline was written and a Run's `wait_deadline_at` is a
        copy of it. One source, so the sweep and the person clicking cannot
        disagree about whether there was still time.
        """
        rows = await self._session.execute(
            select(ApprovalRow.id, ApprovalRow.run_id)
            .where(
                ApprovalRow.status == ApprovalStatus.PENDING.value,
                ApprovalRow.expires_at <= now,
            )
            .order_by(ApprovalRow.expires_at)
            .limit(limit)
        )
        return [(row.id, row.run_id) for row in rows.all()]

    async def expire_approval(
        self, approval_id: UUID, run_id: UUID, request_id: str, now: datetime
    ) -> None:
        """Nobody answered. The row says so and the Run pauses.

        The Run is moved only if it is still waiting: it may have been
        cancelled, or answered a moment ago by somebody whose click and this
        sweep raced. Forcing the transition would turn that race into an error
        for the person who clicked in time.
        """
        approval = await self._session.get(ApprovalRow, approval_id)
        if approval is None or approval.status != ApprovalStatus.PENDING.value:
            return
        approval.status = ApprovalStatus.EXPIRED.value
        approval.decided_at = now
        await self._session.flush()
        run = await self._lock_run_any_workspace(run_id)
        if run is None or RunState(run.status) is not RunState.WAITING_APPROVAL:
            return
        session = await self._lock_session(run.workspace_id, run.session_id)
        if session is None:  # pragma: no cover - a Run always has one
            return
        await self._decide_and_write(
            run,
            session,
            RunSignal.APPROVAL_PAUSED,
            pause_reason=PauseReason.APPROVAL_EXPIRED,
            wait_kind=None,
            wait_deadline_at=None,
            request_id=request_id,
            payload={"approval_id": str(approval_id)},
        )

    async def aged_compat_timeout_runs(self, now: datetime, limit: int) -> Sequence[UUID]:
        cutoff = now - timedelta(hours=24)
        rows = await self._session.scalars(
            select(RunRow.id)
            .where(
                RunRow.status == RunState.PAUSED.value,
                RunRow.pause_reason == PauseReason.COMPAT_TIMEOUT.value,
                RunRow.updated_at <= cutoff,
            )
            .order_by(RunRow.updated_at)
            .limit(limit)
        )
        return list(rows.all())

    async def cancel_aged_compat_timeout(self, run_id: UUID, request_id: str) -> None:
        run = await self._lock_run_any_workspace(run_id)
        if run is None or RunState(run.status) is not RunState.PAUSED:
            return
        if run.pause_reason != PauseReason.COMPAT_TIMEOUT.value:
            return
        session = await self._lock_session(run.workspace_id, run.session_id)
        if session is None:
            return
        await self._decide_and_write(
            run,
            session,
            RunSignal.CANCEL_REQUESTED,
            pause_reason=None,
            wait_kind=None,
            wait_deadline_at=None,
            request_id=request_id,
            payload={"reason": "compat_timeout_aged_out"},
        )

    async def parents_awaiting_children(self, limit: int) -> Sequence[UUID]:
        """Parents hanging on children, oldest wait first.

        Every one of them, not only the settled ones: whether a wait is
        satisfied is `settle_child_wait`'s question and it needs the Run locked
        to answer it. A scan that pre-filtered would be reading the same rows
        twice and deciding on the first read.
        """
        rows = await self._session.scalars(
            select(RunRow.id)
            .where(
                RunRow.status == RunState.WAITING_EXTERNAL.value,
                RunRow.wait_kind == WAIT_CHILD_RUNS,
            )
            .order_by(RunRow.updated_at)
            .limit(limit)
        )
        return list(rows.all())

    async def settle_child_wait(self, parent_run_id: UUID, request_id: str) -> bool:
        """Hand over what the children finished, and wake the parent if it is time.

        Returns whether the parent went back to the queue.

        §13's tenth clause in one place, because its three outcomes are one
        decision and splitting them would let two of them drift:

        - `all` waits until no child is still going.
        - `any` wakes on the first **success** and cancels the rest, which is
          the section's default. The cancelled siblings are still spending the
          root budget, and continuing to pay for an answer nobody will read is
          the thing that default exists to stop.
        - Every child terminal with no success is **not** an error. The parent
          goes back to the queue holding a failure summary it can read and act
          on — §13 is explicit that it is told, not that it fails.

        Delivery and the wake are one transaction, and `result_delivered_at` is
        stamped in it. A crash between them would otherwise either lose an
        answer or hand it over twice.
        """
        parent = await self._lock_run_any_workspace(parent_run_id)
        if parent is None or RunState(parent.status) is not RunState.WAITING_EXTERNAL:
            return False
        if parent.wait_kind != WAIT_CHILD_RUNS:  # pragma: no cover - scanned for
            return False
        children = list(
            (
                await self._session.scalars(
                    select(RunRow)
                    .where(
                        RunRow.parent_run_id == parent.id,
                        RunRow.result_delivered_at.is_(None),
                    )
                    .order_by(RunRow.created_at, RunRow.id)
                    .with_for_update()
                )
            ).all()
        )
        if not children:  # pragma: no cover - a wait always has children
            return False
        policy = WaitPolicy(parent.wait_policy or WaitPolicy.ALL.value)
        finished = [
            child for child in children if RunState(child.status) in TERMINAL_STATES
        ]
        succeeded = [
            child for child in finished if RunState(child.status) is RunState.COMPLETED
        ]
        if policy is WaitPolicy.ANY and succeeded:
            # Cancel the rest before delivering, so the answer the parent reads
            # already reflects what happened to its siblings.
            for child in children:
                if RunState(child.status) not in TERMINAL_STATES:
                    await self._cancel_child(child, request_id, "sibling_succeeded")
            finished = [
                child
                for child in children
                if RunState(child.status) in TERMINAL_STATES
            ]
        elif len(finished) != len(children):
            # `all`, and somebody is still working. Nothing is delivered
            # piecemeal: a parent handed one of three answers would be a parent
            # woken by a wait it did not ask for.
            return False

        session = await self._lock_session(parent.workspace_id, parent.session_id)
        if session is None:  # pragma: no cover - a Run always has one
            return False
        await self._deliver_child_results(parent, session, finished)
        await self._decide_and_write(
            parent,
            session,
            RunSignal.EXTERNAL_READY,
            pause_reason=None,
            wait_kind=None,
            wait_deadline_at=None,
            request_id=request_id,
            payload={
                "reason": "children_settled",
                "wait": policy.value,
                "children": len(finished),
                "succeeded": len(succeeded),
            },
        )
        return True

    async def _deliver_child_results(
        self, parent: RunRow, session: SessionRow, children: Sequence[RunRow]
    ) -> None:
        """Append one platform turn carrying every child's report, once.

        One turn rather than one per child: they are the answer to a single
        question the parent asked, and a conversation in which they arrive as
        separate messages is one where a context planner may trim half of them
        and leave the parent believing it heard from everybody.
        """
        now = datetime.now(UTC)
        lines: list[str] = []
        for child in children:
            report = child.delegation_result or {}
            status = str(report.get("status", child.status))
            summary = str(report.get("summary", "")).strip()
            reason = report.get("failure_reason")
            said = summary or "It reported nothing."
            if status != RunState.COMPLETED.value:
                said = f"{said} (reason: {reason})" if reason else said
            handed = [str(item) for item in report.get("artifacts", [])]
            for artifact_id in handed:
                await self._grant_artifact(
                    parent.workspace_id, UUID(artifact_id), parent.id, "delivered_up"
                )
            if handed:
                said = f"{said} Files: {', '.join(handed)}."
            lines.append(f"- {child.id} [{status}]: {said}")
            child.result_delivered_at = now
        body = (
            "The Agents you delegated to have finished. This is everything they "
            "reported; you cannot see how they worked.\n" + "\n".join(lines)
        )
        self._session.add(
            SessionMessageRow(
                id=uuid4(),
                session_id=session.id,
                workspace_id=parent.workspace_id,
                sequence=session.next_message_sequence,
                role="user",
                # The platform's own words relaying somebody else's work, which
                # is exactly what `author` exists to distinguish from something
                # a person typed.
                content=CanonicalMessage(
                    role="user",
                    blocks=(TextBlock(text=body),),
                    author="platform",
                ).document(),
                source_run_id=parent.id,
                redacted=False,
                created_at=now,
            )
        )
        session.next_message_sequence += 1
        await self._session.flush()

    async def _cancel_child(
        self, child: RunRow, request_id: str, reason: str
    ) -> None:
        """Stop one child, whatever it is doing.

        Used by `any`'s default and by a cancelled parent's cascade. A child
        already terminal is left alone rather than refused: both callers are
        sweeps, and racing a Run that finished a moment ago is ordinary.
        """
        if RunState(child.status) in TERMINAL_STATES:
            return
        session = await self._lock_session(child.workspace_id, child.session_id)
        if session is None:  # pragma: no cover - a Run always has one
            return
        await self._decide_and_write(
            child,
            session,
            RunSignal.CANCEL_REQUESTED,
            pause_reason=None,
            wait_kind=None,
            wait_deadline_at=None,
            request_id=request_id,
            payload={"reason": reason},
        )

    async def cancelled_parents_with_children(self, limit: int) -> Sequence[UUID]:
        """Terminated parents that still have a child going (§13's eleventh clause).

        Read as a scan rather than done inside the parent's own transition for
        one reason: a parent can reach a terminal state down several paths —
        cancelled by a person, failed, timed out — and a cascade attached to
        one of them is a cascade the others do not get.
        """
        child = aliased(RunRow)
        rows = await self._session.scalars(
            select(RunRow.id)
            .join(child, child.parent_run_id == RunRow.id)
            .where(
                RunRow.status.in_([state.value for state in TERMINAL_STATES]),
                child.status.not_in([state.value for state in TERMINAL_STATES]),
            )
            .distinct()
            .limit(limit)
        )
        return list(rows.all())

    async def cascade_cancel_children(self, parent_run_id: UUID, request_id: str) -> int:
        """Cancel every child of a parent that is no longer going anywhere.

        §13's eleventh clause. A child outliving its parent is a Run spending
        the root budget on work whose only reader has gone.
        """
        parent = await self._lock_run_any_workspace(parent_run_id)
        if parent is None or RunState(parent.status) not in TERMINAL_STATES:
            return 0
        children = (
            await self._session.scalars(
                select(RunRow)
                .where(
                    RunRow.parent_run_id == parent.id,
                    RunRow.status.not_in([state.value for state in TERMINAL_STATES]),
                )
                .with_for_update()
            )
        ).all()
        for child in children:
            await self._cancel_child(child, request_id, "parent_terminated")
        return len(children)

    async def time_out_external_wait(self, run_id: UUID, request_id: str) -> None:
        run = await self._lock_run_any_workspace(run_id)
        if run is None or RunState(run.status) is not RunState.WAITING_EXTERNAL:
            return
        session = await self._lock_session(run.workspace_id, run.session_id)
        if session is None:
            return
        await self._decide_and_write(
            run,
            session,
            RunSignal.EXTERNAL_PAUSED,
            pause_reason=PauseReason.EXTERNAL_TIMEOUT,
            wait_kind=None,
            wait_deadline_at=None,
            request_id=request_id,
            payload={"reason": "wait_deadline_passed"},
        )

    async def delete_expired_idempotency_records(
        self, now: datetime, limit: int
    ) -> int:
        doomed = (
            select(IdempotencyRecordRow.id)
            .where(
                IdempotencyRecordRow.expires_at.is_not(None),
                IdempotencyRecordRow.expires_at <= now,
            )
            .limit(limit)
            .scalar_subquery()
        )
        removed = await self._session.scalars(
            delete(IdempotencyRecordRow)
            .where(IdempotencyRecordRow.id.in_(doomed))
            .returning(IdempotencyRecordRow.id)
            .execution_options(synchronize_session=False)
        )
        return len(removed.all())

    async def prune_terminal_run_events(self, before: datetime, limit: int) -> int:
        """Drop old events for finished Runs only.

        A live subscriber must never lose an event it can still legitimately
        ask for, so non-terminal Runs keep everything.
        """
        doomed = (
            select(RunEventRow.id)
            .join(RunRow, RunRow.id == RunEventRow.run_id)
            .where(
                RunRow.status.in_([state.value for state in TERMINAL_STATES]),
                RunEventRow.occurred_at < before,
            )
            .limit(limit)
            .scalar_subquery()
        )
        pruned = await self._session.scalars(
            delete(RunEventRow)
            .where(RunEventRow.id.in_(doomed))
            .returning(RunEventRow.id)
            .execution_options(synchronize_session=False)
        )
        return len(pruned.all())

    async def _lock_run_any_workspace(self, run_id: UUID) -> RunRow | None:
        return await self._session.scalar(
            select(RunRow).where(RunRow.id == run_id).with_for_update()
        )

    async def _release_lease(self, run_id: UUID) -> None:
        await self._session.execute(
            update(WorkerLeaseRow)
            .where(WorkerLeaseRow.run_id == run_id, WorkerLeaseRow.released_at.is_(None))
            .values(released_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )

    async def repair_session_head(
        self, session_id: UUID, request_id: str
    ) -> RepairResult:
        """Recompute one Session head; write nothing when nothing was wrong.

        The single-Session lock is the seam a phase-2B Scheduler will wrap in a
        global advisory lock. This slice starts no loop and takes no such lock.
        """
        session = await self._session.scalar(
            select(SessionRow).where(SessionRow.id == session_id).with_for_update()
        )
        if session is None:
            raise UnknownSession

        candidates = (
            await self._session.scalars(
                select(RunRow)
                .where(
                    RunRow.session_id == session_id,
                    RunRow.status.not_in([state.value for state in TERMINAL_STATES]),
                )
                .order_by(RunRow.session_sequence)
                .with_for_update()
            )
        ).all()
        expected_head = candidates[0].id if candidates else None

        now = datetime.now(UTC)
        changed = session.head_run_id != expected_head
        for index, run in enumerate(candidates):
            expected_blocker = None if index == 0 else expected_head
            if run.blocked_by_run_id != expected_blocker:
                run.blocked_by_run_id = expected_blocker
                run.updated_at = now
                changed = True
        session.head_run_id = expected_head
        if not changed:
            return RepairResult(session_id=session_id, changed=False, head_run_id=None)

        await self._session.flush()
        if expected_head is not None:
            await self.append_events(
                AppendEventsCommand(
                    workspace_id=session.workspace_id,
                    run_id=expected_head,
                    events=(ReservedEvent(RunEventType.SESSION_HEAD_REPAIRED, {}),),
                )
            )
        self._audit(
            session.workspace_id,
            None,
            "session.head_repaired",
            "session",
            session_id,
            request_id,
        )
        return RepairResult(
            session_id=session_id, changed=True, head_run_id=expected_head
        )

    async def derive_retry(self, command: RetryRunCommand) -> AcceptedRun:
        """Create one Derived Retry that shares the failed Run's root budget.

        Idempotency is claimed first, then the source Run, its Session, and the
        single root budget row are locked in that order so concurrent retries
        serialize on one counter.
        """
        if not command.capabilities.can_retry:
            raise ForbiddenRunAction
        replay = await self._claim_idempotency(
            command.workspace_id,
            command.caller.caller_type,
            command.caller.caller_id,
            command.endpoint,
            command.idempotency_key,
            command.request_fingerprint,
        )
        if isinstance(replay, AcceptedRun):
            return replay

        source = await self._lock_run(command.workspace_id, command.source_run_id)
        if source is None:
            raise UnknownRun
        session = await self._lock_session(command.workspace_id, source.session_id)
        if session is None:
            raise UnknownSession
        budget = await self._session.scalar(
            select(RunBudgetScopeRow)
            .where(RunBudgetScopeRow.root_run_id == source.budget_root_run_id)
            .with_for_update()
        )
        if budget is None:
            raise UnknownRun

        blocker = await self._retry_blocker(source, session, _budget_summary(budget))
        if blocker is not None:
            raise RETRY_ERRORS[blocker]

        consumed = await self._session.scalar(
            update(RunBudgetScopeRow)
            .where(
                RunBudgetScopeRow.root_run_id == budget.root_run_id,
                RunBudgetScopeRow.version == budget.version,
                RunBudgetScopeRow.derived_retry_count < RunBudgetScopeRow.max_derived_retries,
            )
            .values(
                derived_retry_count=RunBudgetScopeRow.derived_retry_count + 1,
                version=RunBudgetScopeRow.version + 1,
            )
            .returning(RunBudgetScopeRow.derived_retry_count)
            .execution_options(synchronize_session=False)
        )
        if consumed is None:
            raise RetryLimitReached
        await self._session.refresh(budget, ["derived_retry_count", "version"])

        now = datetime.now(UTC)
        run_id = uuid4()
        run = RunRow(
            id=run_id,
            workspace_id=command.workspace_id,
            session_id=session.id,
            agent_version_id=source.agent_version_id,
            status=RunState.QUEUED.value,
            state_version=1,
            next_event_sequence=1,
            session_sequence=session.next_run_sequence,
            blocked_by_run_id=session.head_run_id,
            retry_of_run_id=source.id,
            budget_root_run_id=source.budget_root_run_id,
            checkpoint=source.checkpoint,
            checkpoint_replay_safe=True,
            checkpoint_effect_status=CheckpointEffectStatus.NONE.value,
            checkpoint_workspace_revision_id=session.workspace_revision_id,
            delivery_mode=source.delivery_mode,
            # Carried from the Run being retried rather than from whoever asked
            # for the retry: the confirmations this Run may need are about the
            # original caller's data, and an operator retrying somebody's work
            # does not become the person who may confirm it.
            end_user_id=source.end_user_id,
            # The price the original was measured at. A retry is the same work
            # again, and repricing it because a rate changed in between would
            # make one Run's cost depend on when it happened to fail.
            model_pricing_version_id=source.model_pricing_version_id,
            created_at=now,
            updated_at=now,
        )
        self._session.add(run)
        await self._session.flush()

        await self.copy_checkpoint_messages(session, source, run_id, now)
        session.next_run_sequence += 1
        if session.head_run_id is None:
            session.head_run_id = run_id
        await self._session.flush()

        await self.append_events(
            AppendEventsCommand(
                workspace_id=command.workspace_id,
                run_id=source.id,
                events=(
                    ReservedEvent(
                        RunEventType.RUN_RETRY_DERIVED, {"derived_run_id": str(run_id)}
                    ),
                ),
            )
        )
        await self.append_events(
            AppendEventsCommand(
                workspace_id=command.workspace_id,
                run_id=run_id,
                events=(
                    ReservedEvent(
                        RunEventType.RUN_CREATED, {"retry_of_run_id": str(source.id)}
                    ),
                ),
            )
        )
        self._audit(
            command.workspace_id,
            command.caller.caller_id,
            "run.retry_derived",
            "run",
            run_id,
            command.request_id,
            actor_type=command.caller.caller_type.value,
        )

        document = (await self._snapshot(run, command.capabilities)).document()
        await self._store_response(
            command.workspace_id,
            command.caller.caller_type,
            command.caller.caller_id,
            command.endpoint,
            command.idempotency_key,
            run_id,
            document,
        )
        return AcceptedRun(run_id=run_id, document=document, replayed=False)

    async def delegate_children(
        self, *, parent_run_id: UUID, requests: tuple[DelegationRequest, ...]
    ) -> DelegationResult:
        """Create one child Run per request, or none of them (§13).

        Every refusal is decided before the first row is written, so the "all
        or none" this returns is a property of the order rather than of a
        rollback. A parent told it delegated three pieces of work and given two
        would sit waiting for a piece nobody is doing.

        Three of §13's clauses are settled here and each is settled from a row
        rather than from an argument:

        **Depth.** The caller's own `depth` decides whether it may delegate at
        all. A child Agent asking is refused here even if its published spec
        somehow carries a delegation policy — §13's third clause is about the
        creation path, not about what a spec happens to bind. The CHECK
        constraint behind it is the second answer to the same question.

        **Scope.** `granted` recomputes the intersection of the parent's own
        scope and the binding, and the result is written onto the child as a
        snapshot. Publishing already refused a binding wider than its parent;
        this is that answer computed again at the moment it becomes a Run,
        because between publishing and running is where a scope could drift.

        **Budget.** The child gets the parent's `budget_root_run_id` and **no
        budget row of its own**. One tree is one set of counters, and there is
        no way to spell "reset" here because there is nothing to reset.

        Its own Session, and therefore its own SessionWorkspace, because those
        are keyed by Session — §13's eighth clause is a shape rather than a
        check, and this is where the shape is chosen.
        """
        parent = await self._session.get(RunRow, parent_run_id)
        if parent is None:  # pragma: no cover - the Worker holds this Run
            return DelegationResult(refusal="this Run is not on record")
        if parent.depth >= MAX_DELEGATION_DEPTH:
            # §13's third clause. Refused on the caller's depth rather than on
            # anything about the children, because that is the fact that makes
            # it a grandchild.
            return DelegationResult(
                refusal=(
                    "you were delegated this work yourself, and an Agent working "
                    "on somebody else's behalf cannot delegate further"
                )
            )
        version = await self._session.get(AgentVersionRow, parent.agent_version_id)
        owning = await self._session.get(SessionRow, parent.session_id)
        if version is None or owning is None:  # pragma: no cover - held by the Worker
            return DelegationResult(refusal="this Run is not on record")
        spec = AgentSpec.model_validate(version.spec)
        policy = spec.delegation
        if policy is None:
            return DelegationResult(refusal="you are not configured to delegate to anybody")
        bindings = {child.alias: child for child in policy.children}
        unbound = sorted({item.alias for item in requests} - set(bindings))
        if unbound:
            return DelegationResult(
                refusal=(
                    f"you may not delegate to {', '.join(unbound)}. "
                    f"You may delegate to {', '.join(sorted(bindings))}"
                )
            )
        if len(requests) > policy.max_parallel:
            return DelegationResult(
                refusal=(
                    f"you asked for {len(requests)} at once and may run "
                    f"{policy.max_parallel}"
                )
            )
        agents = await self._child_agents(
            parent.workspace_id, tuple(bindings[item.alias].alias for item in requests)
        )
        missing = sorted({item.alias for item in requests} - set(agents))
        if missing:
            # Bound at publish and unpublished since. The parent is told which
            # one rather than that something went wrong, because it may be able
            # to do the work itself.
            return DelegationResult(
                refusal=f"{', '.join(missing)} is not published in this workspace"
            )

        wanted = {name for item in requests for name in item.artifacts}
        readable = await self._readable_artifacts(parent, wanted)
        unreadable = sorted(wanted - set(readable))
        if unreadable:
            # §13's eighth clause from the other side: a parent may pass on
            # what it can read and nothing else. Refused before any child
            # exists, so a delegation is never half granted.
            return DelegationResult(
                refusal=(
                    f"you cannot read {', '.join(unreadable)}, so you cannot "
                    f"pass it on"
                )
            )

        now = datetime.now(UTC)
        created: list[DelegatedChild] = []
        for item in requests:
            # The files face is filled in here rather than by `granted`: these
            # are runtime references the spec never bound, which is exactly
            # what `scope_of_spec` says it leaves to be decided where it is
            # used. This is that place.
            scope = replace(
                granted(spec, bindings[item.alias]),
                files=frozenset(str(readable[name]) for name in item.artifacts),
            )
            child_version_id = agents[item.alias]
            child_session = SessionRow(
                id=uuid4(),
                workspace_id=parent.workspace_id,
                agent_id=await self._agent_of_version(child_version_id),
                # Ephemeral: a child Session holds one Run and nobody continues
                # it. A persistent one would suggest there is a conversation
                # here to come back to, and there is not — the parent reads a
                # result, not a thread.
                session_mode=SessionMode.EPHEMERAL.value,
                # §13's fourth clause: the child inherits the calling subject
                # for identity, audit and data ownership. It does **not**
                # inherit the parent Agent's private memories, and this is why
                # it cannot — the memory scope is workspace + agent + subject,
                # and the agent here is the child's own.
                caller_type=owning.caller_type,
                caller_id=owning.caller_id,
                head_run_id=None,
                next_run_sequence=1,
                next_message_sequence=1,
                workspace_revision_id=None,
                created_at=now,
            )
            self._session.add(child_session)
            await self._session.flush()

            child_id = uuid4()
            child = RunRow(
                id=child_id,
                workspace_id=parent.workspace_id,
                session_id=child_session.id,
                agent_version_id=child_version_id,
                status=RunState.QUEUED.value,
                state_version=1,
                next_event_sequence=1,
                session_sequence=1,
                blocked_by_run_id=None,
                parent_run_id=parent.id,
                depth=parent.depth + 1,
                delegation_scope=scope.document(),
                # Red line four: the root, never a new one. Creating a child
                # spends the same Token, cost, time, tool-call and retry
                # counters the parent has already been spending.
                budget_root_run_id=parent.budget_root_run_id,
                checkpoint_replay_safe=True,
                checkpoint_effect_status=CheckpointEffectStatus.NONE.value,
                checkpoint_workspace_revision_id=None,
                # Carried from the parent: a confirmation a child needs is
                # about the same person's data, and a child with no EndUser
                # would be unable to ask anybody.
                end_user_id=parent.end_user_id,
                # The child's own endpoint at today's price. Not the parent's:
                # they may be different models, and measuring one at the
                # other's rate would put a number on this Run that is about
                # somebody else's.
                model_pricing_version_id=await self._current_pricing(child_version_id),
                created_at=now,
                updated_at=now,
            )
            self._session.add(child)
            await self._session.flush()

            for name in item.artifacts:
                await self._grant_artifact(
                    parent.workspace_id, readable[name], child_id, "delegated_down"
                )

            self._session.add(
                SessionMessageRow(
                    id=uuid4(),
                    session_id=child_session.id,
                    workspace_id=parent.workspace_id,
                    sequence=1,
                    role="user",
                    # `platform` rather than unattributed: §13's seventh clause
                    # keeps the parent's transcript out of the child, so this
                    # turn is the platform relaying a delegation and not
                    # something a person typed.
                    content=CanonicalMessage(
                        role="user",
                        blocks=(TextBlock(text=item.instruction),),
                        author="platform",
                    ).document(),
                    source_run_id=child_id,
                    redacted=False,
                    created_at=now,
                )
            )
            child_session.head_run_id = child_id
            child_session.next_run_sequence = 2
            child_session.next_message_sequence = 2
            await self._session.flush()

            await self.append_events(
                AppendEventsCommand(
                    workspace_id=parent.workspace_id,
                    run_id=child_id,
                    events=(
                        ReservedEvent(
                            RunEventType.RUN_CREATED,
                            {
                                "parent_run_id": str(parent.id),
                                "depth": child.depth,
                                "delegation_scope": scope.document(),
                            },
                        ),
                    ),
                )
            )
            self._audit(
                parent.workspace_id,
                owning.caller_id,
                "run.delegated",
                "run",
                child_id,
                f"delegate-{parent.id}",
                actor_type=owning.caller_type,
            )
            created.append(
                DelegatedChild(
                    run_id=child_id, session_id=child_session.id, alias=item.alias
                )
            )
        return DelegationResult(children=tuple(created))

    async def _readable_artifacts(
        self, run: RunRow, named: set[str]
    ) -> dict[str, UUID]:
        """Which of these ids this Run may actually read, keyed by what it typed.

        Two ways in, and no third: an Artifact this Run produced itself, or one
        somebody granted it. That is the whole reachability rule, and it is
        asked here rather than trusted from the caller because this is the only
        place that knows which Run is asking.

        An id that is not a UUID is simply absent from the answer, so a model
        that invented one is told it cannot read it — which is true — rather
        than crashing the round on a parse.
        """
        if not named:
            return {}
        wanted: dict[UUID, str] = {}
        for name in named:
            try:
                wanted[UUID(name)] = name
            except ValueError:
                continue
        if not wanted:
            return {}
        rows = await self._session.execute(
            select(ArtifactRow.id)
            .outerjoin(
                ArtifactGrantRow,
                (ArtifactGrantRow.artifact_id == ArtifactRow.id)
                & (ArtifactGrantRow.run_id == run.id),
            )
            .where(
                ArtifactRow.id.in_(list(wanted)),
                ArtifactRow.workspace_id == run.workspace_id,
                (ArtifactRow.run_id == run.id) | (ArtifactGrantRow.id.is_not(None)),
            )
        )
        return {wanted[found]: found for (found,) in rows.all()}

    async def _grant_artifact(
        self, workspace_id: UUID, artifact_id: UUID, run_id: UUID, reason: str
    ) -> None:
        """Let one Run read one Artifact, once.

        `ON CONFLICT DO NOTHING` rather than a check first: granting twice is
        ordinary — the same file to two children, a redelivered result — and a
        read-then-write here would be a race between two Workers doing exactly
        that.
        """
        await self._session.execute(
            pg_insert(ArtifactGrantRow)
            .values(
                id=uuid4(),
                workspace_id=workspace_id,
                artifact_id=artifact_id,
                run_id=run_id,
                reason=reason,
                created_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(constraint="uq_artifact_grants_pair")
        )

    async def _child_agents(
        self, workspace_id: UUID, aliases: tuple[str, ...]
    ) -> dict[str, UUID]:
        """The published Version behind each alias, in this workspace only.

        Missing aliases are simply absent from the answer. Whoever asked names
        them in the refusal, which is more use to a parent than an exception it
        cannot read.
        """
        if not aliases:
            return {}
        rows = await self._session.execute(
            select(AgentRow.alias, AgentRow.current_version_id).where(
                AgentRow.workspace_id == workspace_id,
                AgentRow.alias.in_(list(set(aliases))),
                AgentRow.current_version_id.is_not(None),
            )
        )
        return {alias: version_id for alias, version_id in rows.all()}

    async def _agent_of_version(self, agent_version_id: UUID) -> UUID:
        version = await self._session.get(AgentVersionRow, agent_version_id)
        if version is None:  # pragma: no cover - just read from the same row
            raise UnknownRun
        return version.agent_id

    async def list_session_messages(
        self, workspace_id: UUID, session_id: UUID
    ) -> Sequence[StoredMessage]:
        # Deliberately not filtered on `withdrawn_at`: this is the transcript a
        # person reads, and a person who took a message back still needs to see
        # that they said it and that it is marked withdrawn.
        #
        # `withdrawn_at` rides along on the DTO so the fact can be rendered.
        # Carrying it here is necessary and not sufficient, and the first cut
        # of this shipped believing otherwise: both response models built from
        # `message.document()`, which has no such key, so the column reached
        # exactly this line and stopped. What makes the fact reachable is the
        # whole chain — this field, `SessionMessageResponse.withdrawn_at`,
        # `EndUserSessionMessageResponse.withdrawn_at`, and the console's own
        # transcript row. Anything that drops it on the way out puts the fact
        # back out of reach, whatever this comment says.
        rows = (
            await self._session.scalars(
                select(SessionMessageRow)
                .where(
                    SessionMessageRow.workspace_id == workspace_id,
                    SessionMessageRow.session_id == session_id,
                    SessionMessageRow.redacted.is_(False),
                )
                .order_by(SessionMessageRow.sequence)
            )
        ).all()
        return [
            StoredMessage(
                id=row.id,
                sequence=row.sequence,
                message=_to_message(row),
                withdrawn_at=row.withdrawn_at,
            )
            for row in rows
        ]

    async def record_end_user_session_read(
        self,
        workspace_id: UUID,
        reader: CallerIdentity,
        end_user_id: UUID,
        session_id: UUID,
        request_id: str,
    ) -> None:
        self._audit(
            workspace_id,
            reader.caller_id,
            "end_user_session.read",
            "session",
            session_id,
            request_id,
            context={"end_user_id": str(end_user_id)},
            actor_type=reader.caller_type.value,
        )

    async def claim_idempotency(
        self,
        workspace_id: UUID,
        caller_type: CallerType,
        caller_id: UUID,
        endpoint: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> AcceptedRun | None:
        return await self._claim_idempotency(
            workspace_id, caller_type, caller_id, endpoint, idempotency_key, fingerprint
        )

    async def store_idempotency_response(
        self,
        workspace_id: UUID,
        caller_type: CallerType,
        caller_id: UUID,
        endpoint: str,
        idempotency_key: str,
        run_id: UUID,
        document: dict[str, Any],
    ) -> None:
        await self._store_response(
            workspace_id,
            caller_type,
            caller_id,
            endpoint,
            idempotency_key,
            run_id,
            document,
        )

    async def copy_checkpoint_messages(
        self, session: SessionRow, source: RunRow, run_id: UUID, now: datetime
    ) -> Sequence[SessionMessageRow]:
        """Re-point the source Run's authorized messages at the Derived Retry.

        Only references are copied; no message content is rewritten and no
        redacted message is exposed.

        Public rather than the private name this method carried before, purely
        as a test seam — production still reaches this only from `derive_retry`.
        Returns what it copied so a caller (a test, here) can check what did
        not make it across.
        """
        rows = (
            await self._session.scalars(
                select(SessionMessageRow)
                .where(
                    SessionMessageRow.session_id == session.id,
                    SessionMessageRow.source_run_id == source.id,
                    SessionMessageRow.redacted.is_(False),
                    # A checkpoint is for continuing. Copying a message the
                    # user withdrew into the new Run's history would hand it
                    # straight back to the model on the very next round —
                    # continuing with the history they took back is the same
                    # as never having taken it back.
                    SessionMessageRow.withdrawn_at.is_(None),
                )
                .order_by(SessionMessageRow.sequence)
            )
        ).all()
        copied: list[SessionMessageRow] = []
        for row in rows:
            new_row = SessionMessageRow(
                id=uuid4(),
                session_id=session.id,
                workspace_id=session.workspace_id,
                sequence=session.next_message_sequence,
                role=row.role,
                content=row.content,
                source_run_id=run_id,
                redacted=False,
                created_at=now,
            )
            self._session.add(new_row)
            copied.append(new_row)
            session.next_message_sequence += 1
        return copied

    async def _claim_idempotency(
        self,
        workspace_id: UUID,
        caller_type: CallerType,
        caller_id: UUID,
        endpoint: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> AcceptedRun | None:
        """Compete for the key first; the database, not a prior read, decides."""
        claimed = await self._session.scalar(
            pg_insert(IdempotencyRecordRow)
            .values(
                id=uuid4(),
                workspace_id=workspace_id,
                caller_type=caller_type.value,
                caller_id=caller_id,
                endpoint=endpoint,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                run_id=None,
                response_snapshot=None,
                expires_at=None,
                created_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(constraint="uq_idempotency_records_scope")
            .returning(IdempotencyRecordRow.id)
        )
        if claimed is not None:
            return None

        existing = await self._read_record(
            workspace_id, caller_type, caller_id, endpoint, idempotency_key
        )
        if existing is None or existing.run_id is None or existing.response_snapshot is None:
            raise IdempotencyKeyReused
        if existing.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused
        return AcceptedRun(
            run_id=existing.run_id, document=existing.response_snapshot, replayed=True
        )

    async def _read_record(
        self,
        workspace_id: UUID,
        caller_type: CallerType,
        caller_id: UUID,
        endpoint: str,
        idempotency_key: str,
    ) -> IdempotencyRecordRow | None:
        return await self._session.scalar(
            select(IdempotencyRecordRow).where(
                IdempotencyRecordRow.workspace_id == workspace_id,
                IdempotencyRecordRow.caller_type == caller_type.value,
                IdempotencyRecordRow.caller_id == caller_id,
                IdempotencyRecordRow.endpoint == endpoint,
                IdempotencyRecordRow.idempotency_key == idempotency_key,
            )
        )

    async def _store_response(
        self,
        workspace_id: UUID,
        caller_type: CallerType,
        caller_id: UUID,
        endpoint: str,
        idempotency_key: str,
        run_id: UUID,
        document: dict[str, Any],
    ) -> None:
        record = await self._read_record(
            workspace_id, caller_type, caller_id, endpoint, idempotency_key
        )
        if record is None:
            raise IdempotencyKeyReused
        record.run_id = run_id
        record.response_snapshot = document
        await self._session.flush()

    async def _lock_run(self, workspace_id: UUID, run_id: UUID) -> RunRow | None:
        return await self._session.scalar(
            select(RunRow)
            .where(RunRow.id == run_id, RunRow.workspace_id == workspace_id)
            .with_for_update()
        )

    async def _lock_session(
        self, workspace_id: UUID, session_id: UUID
    ) -> SessionRow | None:
        return await self._session.scalar(
            select(SessionRow)
            .where(SessionRow.id == session_id, SessionRow.workspace_id == workspace_id)
            .with_for_update()
        )

    async def _current_pricing(self, agent_version_id: UUID) -> UUID | None:
        """The price in force for whatever endpoint this Version names.

        `None` for a deterministic Agent, which reaches no endpoint, and for an
        endpoint nobody has priced. Both mean the same thing downstream — this
        Run's cost cannot be stated — and neither means it was free.
        """
        spec = await self._session.scalar(
            select(AgentVersionRow.spec).where(AgentVersionRow.id == agent_version_id)
        )
        if not spec:
            return None
        policy: dict[str, Any] = spec.get("model_policy") or {}
        endpoint_id = policy.get("endpoint_id")
        if endpoint_id is None:
            return None
        return await self._session.scalar(
            select(ModelPricingVersionRow.id)
            .where(
                ModelPricingVersionRow.endpoint_id == UUID(str(endpoint_id)),
                ModelPricingVersionRow.effective_at <= datetime.now(UTC),
            )
            .order_by(
                ModelPricingVersionRow.effective_at.desc(),
                ModelPricingVersionRow.version_number.desc(),
            )
            .limit(1)
        )

    async def _cost_ceiling(
        self, workspace_id: UUID
    ) -> tuple[Decimal | None, str | None]:
        found = await self._session.execute(
            select(WorkspaceRow.max_run_cost, WorkspaceRow.cost_currency).where(
                WorkspaceRow.id == workspace_id
            )
        )
        row = found.first()
        return (None, None) if row is None else (row[0], row[1])

    async def _published_version(self, session: SessionRow) -> tuple[UUID, AgentLimits]:
        agent = await self._session.scalar(
            select(AgentRow).where(
                AgentRow.id == session.agent_id,
                AgentRow.workspace_id == session.workspace_id,
            )
        )
        if agent is None or agent.current_version_id is None:
            raise AgentNotPublished
        version = await self._session.get(AgentVersionRow, agent.current_version_id)
        if version is None:
            raise AgentNotPublished
        return version.id, AgentSpec.model_validate(version.spec).limits

    async def _snapshot(
        self, run: RunRow, capabilities: RunCapabilities
    ) -> RunSnapshot:
        session = await self._session.get(SessionRow, run.session_id)
        if session is None:
            raise UnknownSession
        budget = await self._session.get(RunBudgetScopeRow, run.budget_root_run_id)
        if budget is None:
            raise UnknownSession
        summary = _budget_summary(budget)
        siblings = (
            await self._session.execute(
                select(RunRow.id, RunRow.status, RunRow.session_sequence)
                .where(RunRow.session_id == run.session_id)
                .order_by(RunRow.session_sequence)
            )
        ).all()

        state = RunState(run.status)
        pending = [
            (row_id, RunState(status))
            for row_id, status, _ in siblings
            if RunState(status) not in TERMINAL_STATES
        ]
        position, queue_status = _queue_position(
            run.id, state, pending, session.head_run_id
        )
        view = RunStateView(
            state=state,
            pause_reason=None if run.pause_reason is None else PauseReason(run.pause_reason),
            wait_kind=run.wait_kind,
            wait_deadline_at=run.wait_deadline_at,
            pause_requested=run.pause_requested_at is not None,
            cancel_requested=run.cancel_requested_at is not None,
            budget_allows_execution=summary.allows_execution(datetime.now(UTC)),
        )
        blocker = await self._retry_blocker(run, session, summary)
        retry_allowed = capabilities.can_retry and blocker is None
        head_status, head_pause, head_wait, head_deadline, head_actions = (
            await self._blocked_head_fields(queue_status, run, capabilities)
        )
        return RunSnapshot(
            id=run.id,
            workspace_id=run.workspace_id,
            session_id=run.session_id,
            agent_version_id=run.agent_version_id,
            state=state,
            state_version=run.state_version,
            session_sequence=run.session_sequence,
            blocked_by_run_id=run.blocked_by_run_id,
            pause_reason=view.pause_reason,
            wait_kind=run.wait_kind,
            wait_deadline_at=run.wait_deadline_at,
            retry_of_run_id=run.retry_of_run_id,
            budget_root_run_id=run.budget_root_run_id,
            parent_run_id=run.parent_run_id,
            depth=run.depth,
            last_event_sequence=run.next_event_sequence - 1,
            queue_position=position,
            queue_status=queue_status,
            budget=summary,
            available_actions=self._machine.available_actions(
                view,
                can_control=capabilities.can_control,
                can_retry=retry_allowed,
                can_hold_budget=capabilities.can_hold_budget,
            ),
            checkpoint_replay_safe=run.checkpoint_replay_safe,
            checkpoint_effect_status=CheckpointEffectStatus(run.checkpoint_effect_status),
            checkpoint_usage_quality=_usage_quality(run.checkpoint),
            failure_reason=_failure_reason(run.checkpoint),
            current_round=_round(run.checkpoint),
            goal_outcome=_goal_outcome(run.checkpoint),
            goal_unmet=_goal_unmet(run.checkpoint),
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            head_status=head_status,
            head_pause_reason=head_pause,
            head_wait_kind=head_wait,
            head_wait_deadline_at=head_deadline,
            queue_available_actions=head_actions,
            children=await self._child_refs(run),
        )

    async def run_tree(
        self, workspace_id: UUID, run_id: UUID, capabilities: RunCapabilities
    ) -> RunTree | None:
        """Every Run sharing this one's budget, from whichever node was asked.

        Anchored on `budget_root_run_id` rather than walked from a parent:
        asked about a child, walking would have to go up first and then back
        down, and a child that is itself a retry has two upward edges. The
        anchor is one column and it is the same answer from every node.

        `workspace_id` is in the where-clause of both queries. A tree cannot
        span workspaces, so this is belt and braces — but the query that
        looked up the root and the query that listed the tree are two
        chances to forget, and §23's first assertion is about exactly the
        route somebody added last.
        """
        del capabilities
        root_id = await self._session.scalar(
            select(RunRow.budget_root_run_id).where(
                RunRow.id == run_id, RunRow.workspace_id == workspace_id
            )
        )
        if root_id is None:
            return None
        rows = (
            await self._session.execute(
                select(
                    RunRow.id,
                    RunRow.status,
                    RunRow.depth,
                    RunRow.parent_run_id,
                    RunRow.retry_of_run_id,
                    RunRow.created_at,
                    RunRow.finished_at,
                )
                .where(
                    RunRow.workspace_id == workspace_id,
                    RunRow.budget_root_run_id == root_id,
                )
                # `id` breaks the tie: two children delegated in one round
                # share a `created_at`, and an order left to the planner puts
                # a tree in a different shape on every read.
                .order_by(RunRow.depth, RunRow.created_at, RunRow.id)
            )
        ).all()
        budget = await self._session.get(RunBudgetScopeRow, root_id)
        if budget is None:
            return None
        return RunTree(
            budget_root_run_id=root_id,
            nodes=tuple(
                TreeNode(
                    id=row.id,
                    status=RunState(row.status),
                    depth=row.depth,
                    parent_run_id=row.parent_run_id,
                    relation=_relation(row.id, root_id, row.retry_of_run_id),
                    created_at=row.created_at,
                    finished_at=row.finished_at,
                )
                for row in rows
            ),
            budget=_budget_summary(budget),
        )

    async def _child_refs(self, run: RunRow) -> tuple[ChildRunRef, ...]:
        """The Runs this one delegated, for the console's task tree.

        Skipped entirely for a Run at depth 1: a child cannot have children,
        so the query would return nothing every time it ran. Most Runs are not
        parents either, but that is not knowable without asking.
        """
        if run.depth >= MAX_DELEGATION_DEPTH:
            return ()
        rows = await self._session.execute(
            select(RunRow.id, RunRow.status)
            .where(RunRow.parent_run_id == run.id)
            .order_by(RunRow.created_at, RunRow.id)
        )
        return tuple(
            ChildRunRef(id=row_id, status=RunState(status))
            for row_id, status in rows.all()
        )

    async def _blocked_head_fields(
        self,
        queue_status: QueueStatus,
        run: RunRow,
        capabilities: RunCapabilities,
    ) -> tuple[RunState | None, PauseReason | None, str | None, datetime | None, tuple[str, ...]]:
        """Name the blocking head only when this snapshot is the blocked pending Run.

        `queue.available_actions` is the head's list for this caller, not this
        pending Run's. The two answers different questions: what can I do to
        the thing in the way, versus what can I do to the thing I just queued.
        """
        empty: tuple[str, ...] = ()
        if queue_status is not QueueStatus.SESSION_BLOCKED or run.blocked_by_run_id is None:
            return None, None, None, None, empty
        head = await self._session.get(RunRow, run.blocked_by_run_id)
        if head is None:
            return None, None, None, None, empty
        budget_row = await self._session.get(RunBudgetScopeRow, head.budget_root_run_id)
        if budget_row is None:
            return None, None, None, None, empty
        head_summary = _budget_summary(budget_row)
        head_state = RunState(head.status)
        head_reason = None if head.pause_reason is None else PauseReason(head.pause_reason)
        head_view = RunStateView(
            state=head_state,
            pause_reason=head_reason,
            wait_kind=head.wait_kind,
            wait_deadline_at=head.wait_deadline_at,
            pause_requested=head.pause_requested_at is not None,
            cancel_requested=head.cancel_requested_at is not None,
            budget_allows_execution=head_summary.allows_execution(datetime.now(UTC)),
        )
        return (
            head_state,
            head_reason,
            head.wait_kind,
            head.wait_deadline_at,
            self._machine.available_actions(
                head_view,
                can_control=capabilities.can_control,
                can_retry=False,
                can_hold_budget=capabilities.can_hold_budget,
            ),
        )

    async def _retry_blocker(
        self, run: RunRow, session: SessionRow, budget: BudgetSummary
    ) -> str | None:
        """Return the design error code that forbids a retry, or None.

        Root-budget limits are reported before Session position, because an
        exhausted shared allowance is the durable reason: once a competing
        retry wins the last slot, the source is also no longer the latest Run,
        and the limit is the answer the caller can act on.
        """
        if RunState(run.status) is not RunState.FAILED:
            return "retry_not_safe"
        if not run.checkpoint_replay_safe:
            return "retry_not_safe"
        effect = CheckpointEffectStatus(run.checkpoint_effect_status)
        if effect is CheckpointEffectStatus.UNKNOWN:
            return "retry_not_safe"
        if budget.derived_retry_count >= budget.max_derived_retries:
            return "retry_limit_reached"
        if not budget.allows_execution(datetime.now(UTC)):
            return "retry_budget_exhausted"
        if budget.consumed_tool_calls >= budget.max_tool_calls:
            return "retry_budget_exhausted"
        latest = await self._session.scalar(
            select(RunRow.session_sequence)
            .where(RunRow.session_id == run.session_id)
            .order_by(RunRow.session_sequence.desc())
            .limit(1)
        )
        if latest is not None and latest != run.session_sequence:
            return "retry_context_stale"
        if session.workspace_revision_id != run.checkpoint_workspace_revision_id:
            return "retry_context_stale"
        return None

    def _audit(
        self,
        workspace_id: UUID,
        actor_id: UUID | None,
        action: str,
        resource_type: str,
        resource_id: UUID,
        request_id: str,
        result: str = "succeeded",
        context: dict[str, Any] | None = None,
        actor_type: str | None = None,
    ) -> None:
        if actor_type is None:
            actor_type = "user" if actor_id is not None else "system"
        self._session.add(
            AuditEventRow(
                workspace_id=workspace_id,
                actor_type=actor_type,
                actor_id=actor_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                result=result,
                request_id=request_id,
                context=context or {},
            )
        )

    async def _forget_cached_sequence(self, run_id: UUID) -> None:
        """Reload the counter the raw allocator statement moved behind the ORM.

        The refresh is eager because an async session cannot lazily reload an
        expired attribute during ordinary attribute access.
        """
        row = await self._session.get(RunRow, run_id)
        if row is not None:
            await self._session.refresh(row, ["next_event_sequence"])


def _accumulate_cost(budget: RunBudgetScopeRow, cost: Cost | None) -> None:
    """Add one round's cost, and let unknown stay unknown.

    §12.4's rule at the only place it can be enforced. A Run that has made no
    calls has genuinely spent nothing, so it starts at a known zero; the first
    round that cannot be priced turns the total unknown, and nothing turns it
    back. Skipping such a round instead would report a total smaller than what
    was actually spent — the one wrong answer a spending figure must never
    give.
    """
    if budget.cost_quality == CostQuality.UNKNOWN.value:
        return
    if cost is None or not cost.known:
        budget.consumed_cost = None
        budget.cost_quality = CostQuality.UNKNOWN.value
        return
    running = Cost(
        amount=budget.consumed_cost if budget.consumed_cost is not None else Decimal(0),
        currency=budget.cost_currency or cost.currency,
        quality=CostQuality(budget.cost_quality),
    )
    total = running.plus(cost)
    budget.consumed_cost = total.amount
    budget.cost_quality = total.quality.value
    if budget.cost_currency is None:
        budget.cost_currency = total.currency


def _new_budget(
    run_id: UUID,
    limits: AgentLimits,
    now: datetime,
    ceiling: tuple[Decimal | None, str | None] = (None, None),
) -> RunBudgetScopeRow:
    """The Run's own copy of every valve it will be measured against.

    The money ceiling is copied like the rest rather than read from the
    workspace each round: a limit that moved underneath a running Run would
    mean the same Run was measured against two different numbers, and neither
    would be the one anybody set.
    """
    max_cost, currency = ceiling
    return RunBudgetScopeRow(
        root_run_id=run_id,
        max_execution_seconds=limits.max_execution_seconds,
        consumed_execution_ms=0,
        max_elapsed_seconds=limits.max_elapsed_seconds,
        elapsed_deadline_at=now + timedelta(seconds=limits.max_elapsed_seconds),
        max_model_calls=limits.max_model_calls,
        consumed_model_calls=0,
        max_tool_calls=limits.max_tool_calls,
        consumed_tool_calls=0,
        max_tokens=None,
        consumed_tokens=0,
        max_derived_retries=limits.max_derived_retries,
        derived_retry_count=0,
        max_cost=max_cost,
        cost_currency=currency,
        # A known zero, because a Run that has made no model call has genuinely
        # spent nothing. The first round that cannot be priced turns this
        # unknown, and nothing turns it back — that is where §12.4's "unknown
        # is not zero" actually bites, rather than here.
        consumed_cost=Decimal(0),
        cost_quality=CostQuality.PROVIDER.value,
        version=1,
    )


def _latest_request(history: Sequence[StoredMessage]) -> str:
    """What this Run was last asked, as the query memories are ranked against.

    The last user turn rather than the whole conversation: what the person just
    said is what this round is about, and ranking against the transcript would
    let an early digression outweigh the current question forever.
    """
    for item in reversed(history):
        if item.message.role == "user":
            return item.message.text
    return ""


def _relation(run_id: UUID, root_id: UUID, retry_of: UUID | None) -> str:
    """Why this Run is in the tree.

    Checked in this order because a retried root is both: it is the budget
    root's own retry, and calling it "root" would hide that the tree holds two
    Runs of the same task. `retry` wins.
    """
    if retry_of is not None:
        return "retry"
    if run_id == root_id:
        return "root"
    return "child"


def _budget_summary(row: RunBudgetScopeRow) -> BudgetSummary:
    return BudgetSummary(
        max_execution_seconds=row.max_execution_seconds,
        consumed_execution_ms=row.consumed_execution_ms,
        max_elapsed_seconds=row.max_elapsed_seconds,
        elapsed_deadline_at=row.elapsed_deadline_at,
        max_model_calls=row.max_model_calls,
        consumed_model_calls=row.consumed_model_calls,
        max_tool_calls=row.max_tool_calls,
        consumed_tool_calls=row.consumed_tool_calls,
        max_tokens=row.max_tokens,
        consumed_tokens=row.consumed_tokens,
        max_derived_retries=row.max_derived_retries,
        derived_retry_count=row.derived_retry_count,
        max_cost=row.max_cost,
        cost_currency=row.cost_currency,
        consumed_cost=row.consumed_cost,
        cost_quality=row.cost_quality,
    )


def _queue_position(
    run_id: UUID,
    state: RunState,
    pending: list[tuple[UUID, RunState]],
    head_run_id: UUID | None,
) -> tuple[int, QueueStatus]:
    if state in TERMINAL_STATES:
        return 0, QueueStatus.TERMINAL
    position = next(
        (index for index, (other, _) in enumerate(pending, start=1) if other == run_id), 0
    )
    if run_id == head_run_id:
        return position, QueueStatus.HEAD
    head_state = next((value for other, value in pending if other == head_run_id), None)
    if head_state is not None and head_state in WAITING_HEAD_STATES:
        return position, QueueStatus.SESSION_BLOCKED
    return position, QueueStatus.PENDING


def _session_snapshot(row: SessionRow) -> SessionSnapshot:
    return SessionSnapshot(
        id=row.id,
        workspace_id=row.workspace_id,
        agent_id=row.agent_id,
        session_mode=SessionMode(row.session_mode),
        caller=CallerIdentity(CallerType(row.caller_type), row.caller_id),
        head_run_id=row.head_run_id,
        next_run_sequence=row.next_run_sequence,
        next_message_sequence=row.next_message_sequence,
        workspace_revision_id=row.workspace_revision_id,
        created_at=row.created_at,
    )


def _denial_code(error: RunStateError) -> str:
    if isinstance(error, RunLimitReached):
        return "retry_budget_exhausted"
    if isinstance(error, InvalidStateMetadata | InvalidStateTransition):
        return "invalid_state_transition"
    return "invalid_state_transition"


def _apply_decision(run: RunRow, decision: StateDecision, now: datetime) -> None:
    """Write exactly what the state machine returned and nothing else."""
    run.status = decision.state.value
    run.pause_reason = None if decision.pause_reason is None else decision.pause_reason.value
    run.wait_kind = decision.wait_kind
    run.wait_deadline_at = decision.wait_deadline_at
    if decision.set_pause_requested:
        run.pause_requested_at = now
    if decision.clear_pause_request:
        run.pause_requested_at = None
    if decision.set_cancel_requested:
        run.cancel_requested_at = now
    if decision.clear_cancel_request:
        run.cancel_requested_at = None
    if decision.starts_execution and run.started_at is None:
        run.started_at = now
    if decision.is_terminal:
        run.finished_at = now
        run.blocked_by_run_id = None


def _slice_payload(executed_ms: int, checkpoint: dict[str, Any] | None) -> dict[str, Any]:
    """What the round's state-change event carries.

    A `run_failed` event used to carry `executed_ms` alone, so a subscriber
    watching the stream learned no more than a poller did: that it failed.
    The reason rides along when there is one.
    """
    payload: dict[str, Any] = {"executed_ms": executed_ms}
    reason = _failure_reason(checkpoint)
    if reason is not None:
        payload["failure_reason"] = reason
    return payload


def _failure_reason(checkpoint: dict[str, Any] | None) -> str | None:
    """Read why the last round failed out of its checkpoint.

    The Worker already writes it there; before this was read, a `failed` Run
    reported 22 fields and not one of them was the reason, so a caller had to
    open the transcript and infer from an exit code, or open the database.
    """
    if not checkpoint:
        return None
    value: Any = checkpoint.get("failure")
    return str(value) if isinstance(value, str) and value else None


def _usage_quality(checkpoint: dict[str, Any] | None) -> str | None:
    """Read the last round's usage quality out of its checkpoint.

    Out of the checkpoint rather than a column of its own: it describes one
    round, the checkpoint is where a round is described, and a column would
    have to be kept in step with it.
    """
    if not checkpoint:
        return None
    value: Any = checkpoint.get("usage_quality")
    return str(value) if isinstance(value, str) else None


def _round(checkpoint: dict[str, Any] | None) -> int | None:
    """Which round the checkpoint describes, counted across the whole Run.

    Written by the Worker from the same number it gave the model, so what a
    reader sees and what the round saw are one count. Absent for the writes
    that record something other than a judged round — a rolled-back commit, a
    slice that ended before any model call.
    """
    if not checkpoint:
        return None
    value: Any = checkpoint.get("round")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _goal_outcome(checkpoint: dict[str, Any] | None) -> str | None:
    if not checkpoint:
        return None
    value: Any = checkpoint.get("goal_outcome")
    return str(value) if isinstance(value, str) and value else None


def _goal_unmet(checkpoint: dict[str, Any] | None) -> tuple[str, ...]:
    """The declared conditions the last judged round did not meet.

    Empty both when everything passed and when the Agent declared nothing to
    check; `goal_outcome` is what tells those apart.
    """
    if not checkpoint:
        return ()
    value: Any = checkpoint.get("goal_unmet")
    if not isinstance(value, list):
        return ()
    names = cast(list[Any], value)
    return tuple(name for name in names if isinstance(name, str))


def _to_message(row: SessionMessageRow) -> CanonicalMessage:
    """Through the document reader, so a stored tool block survives the trip.

    Reconstructing the message here from `row.role` and flattened text would
    silently drop every block this version does understand, which is worse than
    the version that could not represent them at all.
    """
    return message_from_document({"role": row.role, **row.content})


def _text_of(row: SessionMessageRow) -> str:
    """The words of one stored row, for echoing back to whoever just undid it.

    Goes through `_to_message` rather than re-scanning `row.content["parts"]`
    by hand: that is the one place this module already knows how to turn a
    stored document into blocks, and a second reader here could drift from it
    on a future block type.
    """
    return _to_message(row).text


def _scan_lock_key(name: str) -> int:
    """A stable 63-bit advisory lock key for one scan family."""
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") >> 1
