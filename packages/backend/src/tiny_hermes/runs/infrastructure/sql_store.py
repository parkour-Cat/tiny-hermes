import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.agents.domain.models import AgentLimits, AgentSpec, EndpointModelPolicy
from tiny_hermes.agents.infrastructure.tables import AgentRow, AgentVersionRow
from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.http_tools.infrastructure.documents import operation_from_document
from tiny_hermes.http_tools.infrastructure.tables import (
    HttpToolRow,
    HttpToolVersionRow,
)
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
    BoundSkill,
    BudgetSummary,
    CallerIdentity,
    CallerType,
    CanonicalMessage,
    CheckpointEffectStatus,
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
    SessionMode,
    SessionSnapshot,
    StateDecision,
    StoredMessage,
    WorkspaceCleanupTarget,
    event_type_for,
    message_from_document,
)
from tiny_hermes.runs.domain.state_machine import (
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
    SessionMessageRow,
    SessionRow,
    WorkerLeaseRow,
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
    RenewedLease,
    RenewLeaseCommand,
    RepairResult,
    ReservedEvent,
    RetryRunCommand,
    RunEventRecord,
    RunEventWindow,
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

RETRY_ERRORS: dict[str, RunCoordinationError] = {
    "retry_not_safe": RetryNotSafe(),
    "retry_context_stale": RetryContextStale(),
    "retry_budget_exhausted": RetryBudgetExhausted(),
    "retry_limit_reached": RetryLimitReached(),
}

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
            # who started the Run. Through M2 that is the logged-in caller;
            # a ServiceAccount's Run gets none, which is why such an Agent has
            # to have chosen a pre-authorization or a governance approval at
            # publish rather than relying on somebody being there.
            end_user_id=(
                command.caller.caller_id
                if command.caller.caller_type is CallerType.USER
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
        )
        if owning is None or owning.session_mode == SessionMode.EPHEMERAL.value:
            scoped = scoped.where(SessionMessageRow.source_run_id == run.id)
        found = await self._session.scalars(scoped.order_by(SessionMessageRow.sequence))
        spec = AgentSpec.model_validate(version.spec)
        deadline = None
        if run.delivery_mode == DeliveryMode.CHAT_COMPLETIONS.value:
            deadline = run.created_at + timedelta(seconds=spec.delivery.sync_timeout_seconds)
        return ExecutionContext(
            run_id=run.id,
            state_version=run.state_version,
            spec=spec,
            history=tuple(
                StoredMessage(id=row.id, sequence=row.sequence, message=_to_message(row))
                for row in found
            ),
            cancel_requested=run.cancel_requested_at is not None,
            pause_requested=run.pause_requested_at is not None,
            budget=_budget_summary(budget),
            window=await self._context_window(spec),
            compat_deadline_at=deadline,
            skills=await self._bound_skills(spec),
            loaded_skills=await self._loaded_skills(run.id),
            http_operations=await self._bound_operations(spec),
            prices=await self._pinned_prices(run.model_pricing_version_id),
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

    async def _terminalize(self, run: RunRow, session: SessionRow, now: datetime) -> None:
        """Close a Run out and hand the Session to the next eligible Run."""
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

        await self._copy_checkpoint_messages(session, source, run_id, now)
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

    async def list_session_messages(
        self, workspace_id: UUID, session_id: UUID
    ) -> Sequence[CanonicalMessage]:
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
        return [_to_message(row) for row in rows]

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

    async def _copy_checkpoint_messages(
        self, session: SessionRow, source: RunRow, run_id: UUID, now: datetime
    ) -> None:
        """Re-point the source Run's authorized messages at the Derived Retry.

        Only references are copied; no message content is rewritten and no
        redacted message is exposed.
        """
        rows = (
            await self._session.scalars(
                select(SessionMessageRow)
                .where(
                    SessionMessageRow.session_id == session.id,
                    SessionMessageRow.source_run_id == source.id,
                    SessionMessageRow.redacted.is_(False),
                )
                .order_by(SessionMessageRow.sequence)
            )
        ).all()
        for row in rows:
            self._session.add(
                SessionMessageRow(
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
            )
            session.next_message_sequence += 1

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
            last_event_sequence=run.next_event_sequence - 1,
            queue_position=position,
            queue_status=queue_status,
            budget=summary,
            available_actions=self._machine.available_actions(
                view, can_control=capabilities.can_control, can_retry=retry_allowed
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
                head_view, can_control=capabilities.can_control, can_retry=False
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


def _scan_lock_key(name: str) -> int:
    """A stable 63-bit advisory lock key for one scan family."""
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") >> 1
