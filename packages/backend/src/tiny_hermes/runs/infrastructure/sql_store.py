from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.agents.domain.models import AgentLimits, AgentSpec
from tiny_hermes.agents.infrastructure.tables import AgentRow, AgentVersionRow
from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.runs.application.service import (
    AgentNotPublished,
    IdempotencyKeyReused,
    SessionAgentNotFound,
    UnknownSession,
)
from tiny_hermes.runs.domain.models import (
    TERMINAL_STATES,
    BudgetSummary,
    CallerIdentity,
    CallerType,
    CheckpointEffectStatus,
    PauseReason,
    QueueStatus,
    RunCapabilities,
    RunEvent,
    RunEventType,
    RunSnapshot,
    RunState,
    RunStateView,
    SessionMode,
    SessionSnapshot,
)
from tiny_hermes.runs.domain.state_machine import RunStateMachine
from tiny_hermes.runs.infrastructure.tables import (
    IdempotencyRecordRow,
    RunBudgetScopeRow,
    RunEventRow,
    RunRow,
    SessionMessageRow,
    SessionRow,
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
    RepairResult,
    ReservedEvent,
    RetryRunCommand,
)
from tiny_hermes.tenancy.domain.models import Role
from tiny_hermes.tenancy.infrastructure.tables import MembershipRow

RESERVE_SEQUENCES = text(
    "UPDATE runs SET next_event_sequence = next_event_sequence + :count "
    "WHERE id = :run_id AND workspace_id = :workspace_id "
    "RETURNING next_event_sequence - :count AS first_sequence"
)

WAITING_HEAD_STATES = frozenset(
    {RunState.PAUSED, RunState.WAITING_APPROVAL, RunState.WAITING_EXTERNAL}
)


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
        self._session.add(_new_budget(run_id, limits, now))
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
        rows = (
            await self._session.scalars(
                statement.order_by(RunRow.created_at, RunRow.session_sequence, RunRow.id)
            )
        ).all()
        return [await self._snapshot(row, capabilities) for row in rows]

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
        await self._session.flush()
        await self._forget_cached_sequence(command.run_id)
        return tuple(written)

    async def control_run(self, command: ControlRunCommand) -> RunSnapshot:
        raise NotImplementedError("run control arrives with the control transaction")

    async def apply_signal(self, command: ApplySignalCommand) -> RunSnapshot:
        raise NotImplementedError("signal application arrives with the control transaction")

    async def claim_head(self, command: ClaimRunCommand) -> ClaimedRun | None:
        raise NotImplementedError("lease claiming arrives with the coordination seam")

    async def repair_session_head(
        self, session_id: UUID, request_id: str
    ) -> RepairResult:
        raise NotImplementedError("head repair arrives with the coordination seam")

    async def derive_retry(self, command: RetryRunCommand) -> AcceptedRun:
        raise NotImplementedError("derived retries arrive with the shared budget rules")

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

    async def _lock_session(
        self, workspace_id: UUID, session_id: UUID
    ) -> SessionRow | None:
        return await self._session.scalar(
            select(SessionRow)
            .where(SessionRow.id == session_id, SessionRow.workspace_id == workspace_id)
            .with_for_update()
        )

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
            last_event_sequence=run.next_event_sequence - 1,
            queue_position=position,
            queue_status=queue_status,
            budget=summary,
            available_actions=self._machine.available_actions(
                view, can_control=capabilities.can_control, can_retry=retry_allowed
            ),
            checkpoint_replay_safe=run.checkpoint_replay_safe,
            checkpoint_effect_status=CheckpointEffectStatus(run.checkpoint_effect_status),
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
        )

    async def _retry_blocker(
        self, run: RunRow, session: SessionRow, budget: BudgetSummary
    ) -> str | None:
        """Return the design error code that forbids a retry, or None."""
        if RunState(run.status) is not RunState.FAILED:
            return "retry_not_safe"
        if not run.checkpoint_replay_safe:
            return "retry_not_safe"
        effect = CheckpointEffectStatus(run.checkpoint_effect_status)
        if effect is CheckpointEffectStatus.UNKNOWN:
            return "retry_not_safe"
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
        if not budget.allows_execution(datetime.now(UTC)):
            return "retry_budget_exhausted"
        if budget.consumed_tool_calls >= budget.max_tool_calls:
            return "retry_budget_exhausted"
        if budget.derived_retry_count >= budget.max_derived_retries:
            return "retry_limit_reached"
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
    ) -> None:
        self._session.add(
            AuditEventRow(
                workspace_id=workspace_id,
                actor_type="user" if actor_id is not None else "system",
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


def _new_budget(run_id: UUID, limits: AgentLimits, now: datetime) -> RunBudgetScopeRow:
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
        version=1,
    )


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
