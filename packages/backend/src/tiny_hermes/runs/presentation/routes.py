from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, Request, Response, status
from pydantic import BaseModel, Field

from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.identity.application.auth_service import AuthService
from tiny_hermes.identity.presentation.dependencies import (
    SESSION_COOKIE,
    authenticate_browser_user,
    require_workspace_id,
    verify_browser_write,
)
from tiny_hermes.runs.application.service import (
    RunCoordination,
    RunCoordinationError,
)
from tiny_hermes.runs.domain.models import (
    RunSignal,
    RunSnapshot,
    SessionMode,
    SessionSnapshot,
)
from tiny_hermes.runs.presentation.errors import actor_of, as_app_error

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
    checkpoint_usage_quality: str | None
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
            sessions = await runs.list_sessions(workspace_id, actor_of(user))
        except RunCoordinationError as error:
            raise as_app_error(error) from error
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
                actor_of(user),
                payload.agent_id,
                payload.session_mode,
                request.state.request_id,
            )
        except RunCoordinationError as error:
            raise as_app_error(error) from error
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
            found = await runs.get_session(workspace_id, actor_of(user), session_id)
        except RunCoordinationError as error:
            raise as_app_error(error) from error
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
            found = await runs.list_runs(workspace_id, actor_of(user), session_id)
        except RunCoordinationError as error:
            raise as_app_error(error) from error
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
                actor_of(user),
                payload.session_id,
                payload.input,
                idempotency_key,
                request.state.request_id,
            )
        except RunCoordinationError as error:
            raise as_app_error(error) from error
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
            found = await runs.get_run(workspace_id, actor_of(user), run_id)
        except RunCoordinationError as error:
            raise as_app_error(error) from error
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
                actor_of(user),
                run_id,
                signal,
                payload.expected_state_version,
                request.state.request_id,
            )
        except RunCoordinationError as error:
            raise as_app_error(error) from error
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
                actor_of(user),
                run_id,
                idempotency_key,
                request.state.request_id,
            )
        except RunCoordinationError as error:
            raise as_app_error(error) from error
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
