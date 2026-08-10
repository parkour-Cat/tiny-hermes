from collections.abc import Sequence
from uuid import UUID

from tiny_hermes.runs.domain.models import (
    CallerIdentity,
    CallerType,
    CanonicalMessage,
    RunCapabilities,
    RunSignal,
    RunSnapshot,
    SessionMode,
    SessionSnapshot,
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
    RunStore,
)
from tiny_hermes.shared.errors import AuditedDenial
from tiny_hermes.tenancy.domain.models import Actor, Role

WRITERS = {Role.WORKSPACE_ADMIN, Role.DEVELOPER}
READERS = {Role.WORKSPACE_ADMIN, Role.DEVELOPER, Role.VIEWER}

RUNS_ENDPOINT = "POST /api/v1/runs"
RETRY_ENDPOINT = "POST /api/v1/runs/{run_id}/retry"


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
    ) -> AcceptedRun:
        role = await self._require_role(workspace_id, actor, WRITERS)
        key = _require_idempotency_key(idempotency_key)
        message = CanonicalMessage("user", text)
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

    async def _require_role(
        self, workspace_id: UUID, actor: Actor, allowed: set[Role]
    ) -> Role:
        role = await self._store.role_for(workspace_id, actor.id)
        if role is not None:
            if role not in allowed:
                raise ForbiddenRunAction
            return role
        if not actor.is_platform_admin:
            raise ForbiddenRunAction
        return Role.WORKSPACE_ADMIN


def _caller(actor: Actor) -> CallerIdentity:
    return CallerIdentity(CallerType.USER, actor.id)


def _capabilities(role: Role) -> RunCapabilities:
    writable = role in WRITERS
    return RunCapabilities(can_control=writable, can_retry=writable)


def _require_idempotency_key(value: str | None) -> str:
    normalized = (value or "").strip()
    if not normalized or len(normalized) > 255:
        raise IdempotencyKeyRequired
    return normalized
