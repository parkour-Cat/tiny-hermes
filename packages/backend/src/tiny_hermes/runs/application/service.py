from collections.abc import Sequence
from typing import Any
from uuid import UUID

from tiny_hermes.runs.domain.event_cursor import cursor_is_stale
from tiny_hermes.runs.domain.models import (
    CallerIdentity,
    CallerType,
    CanonicalMessage,
    DeliveryMode,
    RunCapabilities,
    RunSignal,
    RunSnapshot,
    SessionMode,
    SessionSnapshot,
    TextBlock,
    fingerprint_request,
)
from tiny_hermes.runs.ports.store import (
    AcceptedRun,
    AcceptRunCommand,
    ApplySignalCommand,
    ClaimedRun,
    ClaimRunCommand,
    ControlRunCommand,
    CreateSessionCommand,
    RepairResult,
    RetryRunCommand,
    RunEventRecord,
    RunEventWindow,
    RunStore,
    WidenBudgetCommand,
)
from tiny_hermes.shared.errors import AuditedDenial
from tiny_hermes.tenancy.domain.models import Actor, Role

WRITERS = {Role.WORKSPACE_ADMIN, Role.DEVELOPER}
READERS = {Role.WORKSPACE_ADMIN, Role.DEVELOPER, Role.VIEWER}
#: Product design §12.3 wants an *authorized* subject for a budget widening,
#: which is a narrower question than who may run work. A developer starts and
#: pauses Runs; deciding that this workspace will spend more than it agreed to
#: is the workspace administrator's.
BUDGET_HOLDERS = {Role.WORKSPACE_ADMIN}

RUNS_ENDPOINT = "POST /api/v1/runs"
RETRY_ENDPOINT = "POST /api/v1/runs/{run_id}/retry"
#: A separate idempotency/fingerprint namespace from `RUNS_ENDPOINT`, on
#: purpose (§5). The two are reached by different subjects through different
#: authorization — a workspace Role for one, the two-gate Agent check for the
#: other — and collapsing them into one endpoint string would let a replayed
#: request from one path be satisfied by a row the other path wrote.
END_USER_RUNS_ENDPOINT = "POST /api/v1/end-user/sessions/{session_id}/runs"


class RunCoordinationError(Exception):
    """Base class for every expected Run Coordination refusal."""


class ForbiddenRunAction(RunCoordinationError):
    pass


class UnknownSession(RunCoordinationError):
    pass


class UnknownRun(RunCoordinationError):
    pass


class SessionAgentNotFound(RunCoordinationError):
    pass


class AgentNotPublished(RunCoordinationError):
    pass


class IdempotencyKeyRequired(RunCoordinationError):
    pass


class IdempotencyKeyReused(RunCoordinationError):
    pass


class EventSequenceConflict(RunCoordinationError):
    pass


class StateVersionConflict(RunCoordinationError):
    pass


class RunEventCursorTooOld(RunCoordinationError):
    """The subscriber's cursor is below the events the platform still retains."""

    def __init__(self, earliest_available_sequence: int) -> None:
        super().__init__(str(earliest_available_sequence))
        self.earliest_available_sequence = earliest_available_sequence


class LeaseLost(RunCoordinationError):
    """The lease this writer held was released, reclaimed, or superseded."""


class DeniedRunControl(RunCoordinationError, AuditedDenial):
    """An illegal control whose audit denial must survive the refusal."""

    def __init__(self, code: str = "invalid_state_transition") -> None:
        super().__init__(code)
        self.code = code


class RetryNotSafe(RunCoordinationError):
    pass


class RetryContextStale(RunCoordinationError):
    pass


class RetryBudgetExhausted(RunCoordinationError):
    pass


class RetryLimitReached(RunCoordinationError):
    pass


class BudgetNotWidened(RunCoordinationError):
    """The value asked for is not above the ceiling already in force.

    Refused rather than accepted as a no-op: an operator who meant to raise a
    ceiling and typed the number already there should find that out now, not
    when the Run stops at the same place a second time.
    """


class RunCoordination:
    """Session and Run rules.

    The service resolves who the caller is and what they may do, then hands one
    whole business command to the store. It never chooses a Run state.
    """

    def __init__(self, store: RunStore) -> None:
        self._store = store

    async def create_session(
        self,
        workspace_id: UUID,
        actor: Actor,
        agent_id: UUID,
        session_mode: SessionMode,
        request_id: str,
    ) -> SessionSnapshot:
        await self._require_role(workspace_id, actor, WRITERS)
        return await self._store.create_session(
            CreateSessionCommand(
                workspace_id=workspace_id,
                agent_id=agent_id,
                caller=_caller(actor),
                session_mode=session_mode,
                request_id=request_id,
            )
        )

    async def get_session(
        self, workspace_id: UUID, actor: Actor, session_id: UUID
    ) -> SessionSnapshot:
        await self._require_role(workspace_id, actor, READERS)
        session = await self._store.get_session(workspace_id, session_id)
        if session is None:
            raise UnknownSession
        return session

    async def create_end_user_session(
        self,
        workspace_id: UUID,
        end_user_id: UUID,
        agent_id: UUID,
        session_mode: SessionMode,
        request_id: str,
    ) -> SessionSnapshot:
        """§5's Session half, for a subject with no workspace Role at all.

        No `_require_role`: an end user is never a member, so that check has
        nothing to look up. Authorization already happened one layer up —
        the caller reaches `agent_id` only by resolving an alias through
        `AgentCatalog.resolve_end_user_agent`'s two gates — so this only
        writes what that resolution already approved. `caller_type=end_user`
        here is what makes this Session's private memory the end user's own
        (`_remembered`'s own docstring) rather than nobody's.
        """
        return await self._store.create_session(
            CreateSessionCommand(
                workspace_id=workspace_id,
                agent_id=agent_id,
                caller=CallerIdentity(CallerType.END_USER, end_user_id),
                session_mode=session_mode,
                request_id=request_id,
            )
        )

    async def list_sessions(
        self, workspace_id: UUID, actor: Actor
    ) -> Sequence[SessionSnapshot]:
        await self._require_role(workspace_id, actor, READERS)
        return await self._store.list_sessions(workspace_id)

    async def submit_run(
        self,
        workspace_id: UUID,
        actor: Actor,
        session_id: UUID,
        text: str,
        idempotency_key: str | None,
        request_id: str,
        delivery_mode: DeliveryMode | None = None,
    ) -> AcceptedRun:
        role = await self._require_role(workspace_id, actor, WRITERS)
        key = _require_idempotency_key(idempotency_key)
        message = CanonicalMessage("user", (TextBlock(text=text),))
        return await self._store.accept_run(
            AcceptRunCommand(
                workspace_id=workspace_id,
                session_id=session_id,
                caller=_caller(actor),
                capabilities=_capabilities(role),
                endpoint=RUNS_ENDPOINT,
                idempotency_key=key,
                request_fingerprint=fingerprint_request(
                    "POST", RUNS_ENDPOINT, workspace_id, session_id, message, None
                ),
                message=message,
                request_id=request_id,
                delivery_mode=None if delivery_mode is None else delivery_mode.value,
            )
        )

    async def submit_end_user_run(
        self,
        workspace_id: UUID,
        end_user_id: UUID,
        session_id: UUID,
        text: str,
        idempotency_key: str | None,
        request_id: str,
    ) -> AcceptedRun:
        """§5's Run half.

        `session_id` must be a Session this same end user started. Checked
        here rather than left implicit: a Session's private-memory subject is
        read off *its own* `caller_type`/`caller_id`, never off whoever is
        submitting a Run against it (`_remembered`'s own docstring), so an end
        user who guessed another end user's `session_id` would otherwise be
        able to speak into that other subject's conversation — and have it
        remembered as theirs.

        `RunCapabilities` is `can_control=True, can_retry=True`
        unconditionally: there is no workspace Role to read one off, and both
        capabilities are about this Run and nobody else's — the same reason
        `_require_role`'s WRITERS/READERS split does not apply here at all.
        """
        session = await self._store.get_session(workspace_id, session_id)
        if session is None:
            raise UnknownSession
        if (
            session.caller.caller_type is not CallerType.END_USER
            or session.caller.caller_id != end_user_id
        ):
            raise ForbiddenRunAction
        key = _require_idempotency_key(idempotency_key)
        message = CanonicalMessage("user", (TextBlock(text=text),))
        return await self._store.accept_run(
            AcceptRunCommand(
                workspace_id=workspace_id,
                session_id=session_id,
                caller=CallerIdentity(CallerType.END_USER, end_user_id),
                capabilities=RunCapabilities(can_control=True, can_retry=True),
                endpoint=END_USER_RUNS_ENDPOINT,
                idempotency_key=key,
                request_fingerprint=fingerprint_request(
                    "POST", END_USER_RUNS_ENDPOINT, workspace_id, session_id, message, None
                ),
                message=message,
                request_id=request_id,
                delivery_mode=None,
            )
        )

    async def retry_run(
        self,
        workspace_id: UUID,
        actor: Actor,
        source_run_id: UUID,
        idempotency_key: str | None,
        request_id: str,
    ) -> AcceptedRun:
        role = await self._require_role(workspace_id, actor, WRITERS)
        key = _require_idempotency_key(idempotency_key)
        return await self._store.derive_retry(
            RetryRunCommand(
                workspace_id=workspace_id,
                source_run_id=source_run_id,
                caller=_caller(actor),
                capabilities=_capabilities(role),
                endpoint=RETRY_ENDPOINT,
                idempotency_key=key,
                request_fingerprint=fingerprint_request(
                    "POST", RETRY_ENDPOINT, workspace_id, source_run_id, None, None
                ),
                request_id=request_id,
            )
        )

    async def get_run(self, workspace_id: UUID, actor: Actor, run_id: UUID) -> RunSnapshot:
        role = await self._require_role(workspace_id, actor, READERS)
        run = await self._store.get_run(workspace_id, run_id, _capabilities(role))
        if run is None:
            raise UnknownRun
        return run

    async def list_runs(
        self, workspace_id: UUID, actor: Actor, session_id: UUID | None
    ) -> Sequence[RunSnapshot]:
        role = await self._require_role(workspace_id, actor, READERS)
        return await self._store.list_runs(workspace_id, session_id, _capabilities(role))

    async def open_event_stream(
        self, workspace_id: UUID, actor: Actor, run_id: UUID, after_sequence: int
    ) -> RunEventWindow:
        """Admit a subscriber, or refuse before a single frame is written.

        Refusing here keeps a rejection an ordinary Problem Details response
        instead of a half-open stream a browser would have to interpret.
        """
        window = await self.event_window(workspace_id, actor, run_id)
        if cursor_is_stale(after_sequence, window.earliest_sequence, window.next_sequence):
            raise RunEventCursorTooOld(
                window.earliest_sequence
                if window.earliest_sequence is not None
                else window.next_sequence
            )
        return window

    async def event_window(
        self, workspace_id: UUID, actor: Actor, run_id: UUID
    ) -> RunEventWindow:
        await self._require_role(workspace_id, actor, READERS)
        window = await self._store.event_window(workspace_id, run_id)
        if window is None:
            raise UnknownRun
        return window

    async def read_events(
        self,
        workspace_id: UUID,
        actor: Actor,
        run_id: UUID,
        after_sequence: int,
        limit: int,
    ) -> Sequence[RunEventRecord]:
        await self._require_role(workspace_id, actor, READERS)
        return await self._store.list_events_after(
            workspace_id, run_id, after_sequence, limit
        )

    async def control_run(
        self,
        workspace_id: UUID,
        actor: Actor,
        run_id: UUID,
        signal: RunSignal,
        expected_state_version: int,
        request_id: str,
    ) -> RunSnapshot:
        role = await self._require_role(workspace_id, actor, WRITERS)
        return await self._store.control_run(
            ControlRunCommand(
                workspace_id=workspace_id,
                run_id=run_id,
                caller=_caller(actor),
                capabilities=_capabilities(role),
                signal=signal,
                expected_state_version=expected_state_version,
                request_id=request_id,
            )
        )

    async def widen_budget(
        self,
        workspace_id: UUID,
        actor: Actor,
        run_id: UUID,
        expected_state_version: int,
        max_model_calls: int,
        request_id: str,
    ) -> RunSnapshot:
        role = await self._require_role(workspace_id, actor, BUDGET_HOLDERS)
        return await self._store.widen_budget(
            WidenBudgetCommand(
                workspace_id=workspace_id,
                run_id=run_id,
                caller=_caller(actor),
                capabilities=_capabilities(role),
                expected_state_version=expected_state_version,
                max_model_calls=max_model_calls,
                request_id=request_id,
            )
        )

    async def apply_signal(self, command: ApplySignalCommand) -> RunSnapshot:
        """The seam a future Worker or Scheduler process calls.

        It carries no browser identity because those processes act as the
        platform itself, not as a workspace member.
        """
        return await self._store.apply_signal(command)

    async def claim_head_run(self, command: ClaimRunCommand) -> ClaimedRun | None:
        return await self._store.claim_head(command)

    async def repair_session_head(self, session_id: UUID, request_id: str) -> RepairResult:
        return await self._store.repair_session_head(session_id, request_id)

    async def list_session_messages(
        self, workspace_id: UUID, actor: Actor, session_id: UUID
    ) -> Sequence[CanonicalMessage]:
        await self._require_role(workspace_id, actor, READERS)
        if await self._store.get_session(workspace_id, session_id) is None:
            raise UnknownSession
        return await self._store.list_session_messages(workspace_id, session_id)

    async def claim_idempotency(
        self,
        workspace_id: UUID,
        actor: Actor,
        endpoint: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> AcceptedRun | None:
        await self._require_role(workspace_id, actor, WRITERS)
        return await self._store.claim_idempotency(
            workspace_id,
            _caller(actor).caller_type,
            _caller(actor).caller_id,
            endpoint,
            idempotency_key,
            fingerprint,
        )

    async def store_idempotency_response(
        self,
        workspace_id: UUID,
        actor: Actor,
        endpoint: str,
        idempotency_key: str,
        run_id: UUID,
        document: dict[str, Any],
    ) -> None:
        await self._require_role(workspace_id, actor, WRITERS)
        caller = _caller(actor)
        await self._store.store_idempotency_response(
            workspace_id,
            caller.caller_type,
            caller.caller_id,
            endpoint,
            idempotency_key,
            run_id,
            document,
        )

    async def _require_role(
        self, workspace_id: UUID, actor: Actor, allowed: set[Role]
    ) -> Role:
        if actor.is_service_account:
            if actor.role is None or actor.role not in allowed:
                raise ForbiddenRunAction
            return actor.role
        role = await self._store.role_for(workspace_id, actor.id)
        if role is not None:
            if role not in allowed:
                raise ForbiddenRunAction
            return role
        if not actor.is_platform_admin:
            raise ForbiddenRunAction
        return Role.WORKSPACE_ADMIN


def _caller(actor: Actor) -> CallerIdentity:
    kind = CallerType.SERVICE_ACCOUNT if actor.is_service_account else CallerType.USER
    return CallerIdentity(kind, actor.id)


def _capabilities(role: Role) -> RunCapabilities:
    writable = role in WRITERS
    return RunCapabilities(can_control=writable, can_retry=writable)


def _require_idempotency_key(value: str | None) -> str:
    normalized = (value or "").strip()
    if not normalized or len(normalized) > 255:
        raise IdempotencyKeyRequired
    return normalized
