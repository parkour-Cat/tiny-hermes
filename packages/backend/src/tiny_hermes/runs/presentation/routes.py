from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, Request, Response, status
from pydantic import BaseModel, Field

from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.identity.application.auth_service import AuthService
from tiny_hermes.identity.domain.models import AuthenticatedUser
from tiny_hermes.identity.presentation.dependencies import (
    SESSION_COOKIE,
    authenticate_browser_user,
    forbidden,
    require_workspace_id,
    verify_browser_write,
)
from tiny_hermes.runs.application.service import (
    AgentNotPublished,
    DeniedRunControl,
    EventSequenceConflict,
    ForbiddenRunAction,
    IdempotencyKeyRequired,
    IdempotencyKeyReused,
    RetryBudgetExhausted,
    RetryContextStale,
    RetryLimitReached,
    RetryNotSafe,
    RunCoordination,
    RunCoordinationError,
    SessionAgentNotFound,
    StateVersionConflict,
    UnknownRun,
    UnknownSession,
)
from tiny_hermes.runs.domain.models import (
    RunSignal,
    RunSnapshot,
    SessionMode,
    SessionSnapshot,
)
from tiny_hermes.shared.errors import AppError
from tiny_hermes.tenancy.domain.models import Actor

WorkspaceHeader = Annotated[str | None, Header(alias="X-Workspace-Id")]
CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token")]
IdempotencyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]

REPLAYED_HEADER = "Idempotent-Replayed"


class CreateSessionRequest(BaseModel):
    agent_id: UUID
    session_mode: SessionMode = SessionMode.PERSISTENT


class CreateRunRequest(BaseModel):
    session_id: UUID
    input: str = Field(min_length=1, max_length=32_768)


class ControlRunRequest(BaseModel):
    expected_state_version: int = Field(ge=1)


class SessionResponse(BaseModel):
    id: UUID
    agent_id: UUID
    session_mode: str
    caller_type: str
    caller_id: UUID
    head_run_id: UUID | None
    next_run_sequence: int
    next_message_sequence: int
    created_at: datetime

    @classmethod
    def from_domain(cls, session: SessionSnapshot) -> "SessionResponse":
        return cls.model_validate(session.document())


class QueueResponse(BaseModel):
    position: int
    status: str


class RunResponse(BaseModel):
    id: UUID
    session_id: UUID
    agent_version_id: UUID
    status: str
    state_version: int
    session_sequence: int
    blocked_by_run_id: UUID | None
    pause_reason: str | None
    wait_kind: str | None
    wait_deadline_at: datetime | None
    retry_of_run_id: UUID | None
    budget_root_run_id: UUID
    last_event_sequence: int
    queue: QueueResponse
    budget: dict[str, Any]
    available_actions: list[str]
    checkpoint_replay_safe: bool
    checkpoint_effect_status: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_domain(cls, run: RunSnapshot) -> "RunResponse":
        return cls.model_validate(run.document())


def session_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])
    auth_dependency = resources.auth_service
    runs_dependency = resources.run_coordination

    @router.get("", response_model=list[SessionResponse])
    async def list_sessions(  # pyright: ignore[reportUnusedFunction]
        auth: Annotated[AuthService, Depends(auth_dependency)],
        runs: Annotated[RunCoordination, Depends(runs_dependency)],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> list[SessionResponse]:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            sessions = await runs.list_sessions(workspace_id, _actor(user))
        except RunCoordinationError as error:
            raise _as_app_error(error) from error
        return [SessionResponse.from_domain(item) for item in sessions]

    @router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
    async def create_session(  # pyright: ignore[reportUnusedFunction]
        payload: CreateSessionRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency)],
        runs: Annotated[RunCoordination, Depends(runs_dependency)],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> SessionResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            created = await runs.create_session(
                workspace_id,
                _actor(user),
                payload.agent_id,
                payload.session_mode,
                request.state.request_id,
            )
        except RunCoordinationError as error:
            raise _as_app_error(error) from error
        return SessionResponse.from_domain(created)

    @router.get("/{session_id}", response_model=SessionResponse)
    async def get_session(  # pyright: ignore[reportUnusedFunction]
        session_id: UUID,
        auth: Annotated[AuthService, Depends(auth_dependency)],
        runs: Annotated[RunCoordination, Depends(runs_dependency)],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> SessionResponse:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            found = await runs.get_session(workspace_id, _actor(user), session_id)
        except RunCoordinationError as error:
            raise _as_app_error(error) from error
        return SessionResponse.from_domain(found)

    return router


def run_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/runs", tags=["runs"])
    auth_dependency = resources.auth_service
    runs_dependency = resources.run_coordination

    async def _announce(workspace_id: UUID, run_id: UUID) -> None:
        """Tell Workers after the transaction that created work committed."""
        await resources.wake_up_notifier().publish(workspace_id, run_id)

    @router.get("", response_model=list[RunResponse])
    async def list_runs(  # pyright: ignore[reportUnusedFunction]
        auth: Annotated[AuthService, Depends(auth_dependency)],
        runs: Annotated[RunCoordination, Depends(runs_dependency)],
        session_id: UUID | None = None,
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> list[RunResponse]:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            found = await runs.list_runs(workspace_id, _actor(user), session_id)
        except RunCoordinationError as error:
            raise _as_app_error(error) from error
        return [RunResponse.from_domain(item) for item in found]

    @router.post("", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
    async def create_run(  # pyright: ignore[reportUnusedFunction]
        payload: CreateRunRequest,
        request: Request,
        response: Response,
        auth: Annotated[AuthService, Depends(auth_dependency)],
        runs: Annotated[RunCoordination, Depends(runs_dependency)],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
        idempotency_key: IdempotencyHeader = None,
    ) -> RunResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            accepted = await runs.submit_run(
                workspace_id,
                _actor(user),
                payload.session_id,
                payload.input,
                idempotency_key,
                request.state.request_id,
            )
        except RunCoordinationError as error:
            raise _as_app_error(error) from error
        _apply_acceptance_headers(response, accepted.replayed, accepted.run_id)
        if not accepted.replayed:
            await _announce(workspace_id, accepted.run_id)
        return RunResponse.model_validate(accepted.document)

    @router.get("/{run_id}", response_model=RunResponse)
    async def get_run(  # pyright: ignore[reportUnusedFunction]
        run_id: UUID,
        auth: Annotated[AuthService, Depends(auth_dependency)],
        runs: Annotated[RunCoordination, Depends(runs_dependency)],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> RunResponse:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            found = await runs.get_run(workspace_id, _actor(user), run_id)
        except RunCoordinationError as error:
            raise _as_app_error(error) from error
        return RunResponse.from_domain(found)

    async def _control(
        run_id: UUID,
        payload: ControlRunRequest,
        request: Request,
        auth: AuthService,
        runs: RunCoordination,
        signal: RunSignal,
        session_token: str | None,
        csrf_token: str | None,
        selected_workspace: str | None,
    ) -> RunResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            updated = await runs.control_run(
                workspace_id,
                _actor(user),
                run_id,
                signal,
                payload.expected_state_version,
                request.state.request_id,
            )
        except RunCoordinationError as error:
            raise _as_app_error(error) from error
        return RunResponse.from_domain(updated)

    @router.post("/{run_id}/retry", response_model=RunResponse)
    async def retry_run(  # pyright: ignore[reportUnusedFunction]
        run_id: UUID,
        request: Request,
        response: Response,
        auth: Annotated[AuthService, Depends(auth_dependency)],
        runs: Annotated[RunCoordination, Depends(runs_dependency)],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
        idempotency_key: IdempotencyHeader = None,
    ) -> RunResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            accepted = await runs.retry_run(
                workspace_id,
                _actor(user),
                run_id,
                idempotency_key,
                request.state.request_id,
            )
        except RunCoordinationError as error:
            raise _as_app_error(error) from error
        _apply_acceptance_headers(response, accepted.replayed, accepted.run_id)
        if not accepted.replayed:
            await _announce(workspace_id, accepted.run_id)
        return RunResponse.model_validate(accepted.document)

    @router.post("/{run_id}/pause", response_model=RunResponse)
    async def pause_run(  # pyright: ignore[reportUnusedFunction]
        run_id: UUID,
        payload: ControlRunRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency)],
        runs: Annotated[RunCoordination, Depends(runs_dependency)],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> RunResponse:
        return await _control(
            run_id,
            payload,
            request,
            auth,
            runs,
            RunSignal.PAUSE_REQUESTED,
            session_token,
            csrf_token,
            selected_workspace,
        )

    @router.post("/{run_id}/resume", response_model=RunResponse)
    async def resume_run(  # pyright: ignore[reportUnusedFunction]
        run_id: UUID,
        payload: ControlRunRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency)],
        runs: Annotated[RunCoordination, Depends(runs_dependency)],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> RunResponse:
        return await _control(
            run_id,
            payload,
            request,
            auth,
            runs,
            RunSignal.RESUME_REQUESTED,
            session_token,
            csrf_token,
            selected_workspace,
        )

    @router.post("/{run_id}/cancel", response_model=RunResponse)
    async def cancel_run(  # pyright: ignore[reportUnusedFunction]
        run_id: UUID,
        payload: ControlRunRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency)],
        runs: Annotated[RunCoordination, Depends(runs_dependency)],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> RunResponse:
        return await _control(
            run_id,
            payload,
            request,
            auth,
            runs,
            RunSignal.CANCEL_REQUESTED,
            session_token,
            csrf_token,
            selected_workspace,
        )

    return router


def _apply_acceptance_headers(response: Response, replayed: bool, run_id: UUID) -> None:
    if replayed:
        response.status_code = status.HTTP_200_OK
        response.headers[REPLAYED_HEADER] = "true"
        return
    response.status_code = status.HTTP_201_CREATED
    response.headers["Location"] = f"/api/v1/runs/{run_id}"


def _actor(user: AuthenticatedUser) -> Actor:
    return Actor(user.id, user.is_platform_admin)


def _as_app_error(error: RunCoordinationError) -> AppError:
    """Turn Run Coordination refusals into Problem Details without leaking content."""
    if isinstance(error, ForbiddenRunAction):
        return forbidden()
    if isinstance(error, SessionAgentNotFound):
        return _not_found("agent_not_found", "Agent not found", "agent")
    if isinstance(error, UnknownSession):
        return _not_found("session_not_found", "Session not found", "session")
    if isinstance(error, UnknownRun):
        return _not_found("run_not_found", "Run not found", "run")
    if isinstance(error, AgentNotPublished):
        return AppError(
            code="agent_not_published",
            title="Agent not published",
            status=409,
            detail="The agent has no published version to run.",
        )
    if isinstance(error, IdempotencyKeyRequired):
        return AppError(
            code="idempotency_key_required",
            title="Idempotency key required",
            status=400,
            detail="A non-empty Idempotency-Key header is required.",
        )
    if isinstance(error, IdempotencyKeyReused):
        return AppError(
            code="idempotency_key_reused",
            title="Idempotency key reused",
            status=409,
            detail="That idempotency key already belongs to a different request.",
        )
    if isinstance(error, StateVersionConflict):
        return AppError(
            code="state_version_conflict",
            title="Run state version conflict",
            status=409,
            detail="The run changed after it was read.",
        )
    if isinstance(error, DeniedRunControl):
        return AppError(
            code=error.code,
            title="Invalid run control",
            status=409,
            detail="The run cannot accept that control in its current state.",
            audited=True,
        )
    if isinstance(error, EventSequenceConflict):
        return AppError(
            code="event_sequence_conflict",
            title="Run event sequence conflict",
            status=409,
            detail="Concurrent writers could not agree on an event sequence.",
        )
    return _retry_error(error)


def _retry_error(error: RunCoordinationError) -> AppError:
    codes: list[tuple[type[RunCoordinationError], str, str]] = [
        (RetryNotSafe, "retry_not_safe", "The last checkpoint is not safe to replay."),
        (
            RetryContextStale,
            "retry_context_stale",
            "The session moved on after the source run failed.",
        ),
        (
            RetryBudgetExhausted,
            "retry_budget_exhausted",
            "The shared run budget has no remaining capacity.",
        ),
        (
            RetryLimitReached,
            "retry_limit_reached",
            "The shared retry limit is already used up.",
        ),
    ]
    for kind, code, detail in codes:
        if isinstance(error, kind):
            return AppError(
                code=code,
                title=code.replace("_", " ").capitalize(),
                status=409,
                detail=detail,
            )
    return AppError(
        code="run_request_rejected",
        title="Run request rejected",
        status=422,
        detail="The run request could not be completed.",
    )


def _not_found(code: str, title: str, noun: str) -> AppError:
    return AppError(
        code=code,
        title=title,
        status=404,
        detail=f"No such {noun} exists in the selected workspace.",
    )
