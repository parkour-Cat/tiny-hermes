from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, Request, Response, status
from pydantic import BaseModel, Field, model_serializer

from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.artifacts.application.service import ArtifactForbidden, ArtifactService
from tiny_hermes.artifacts.presentation.routes import ArtifactResponse
from tiny_hermes.identity.application.auth_service import AuthService
from tiny_hermes.identity.application.machine_service import MachineIdentityService
from tiny_hermes.identity.presentation.dependencies import (
    SESSION_COOKIE,
    forbidden,
    resolve_workspace_caller,
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
    StoredMessage,
    WorkspaceUsageSummary,
)
from tiny_hermes.runs.presentation.errors import as_app_error

WorkspaceHeader = Annotated[str | None, Header(alias="X-Workspace-Id")]
CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token")]
IdempotencyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]
AuthorizationHeader = Annotated[str | None, Header()]

REPLAYED_HEADER = "Idempotent-Replayed"


class CreateSessionRequest(BaseModel):
    agent_id: UUID
    session_mode: SessionMode = SessionMode.PERSISTENT


class CreateRunRequest(BaseModel):
    session_id: UUID
    input: str = Field(min_length=1, max_length=32_768)


class ControlRunRequest(BaseModel):
    expected_state_version: int = Field(ge=1)


class WidenBudgetRequest(BaseModel):
    """Product design §12.3's explicit act, spelled out as one.

    The new ceiling is named in full rather than as an increment, so the value
    the operator approved is the value the audit row records.
    """

    expected_state_version: int = Field(ge=1)
    max_model_calls: int = Field(ge=1)


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


class SessionMessageResponse(BaseModel):
    role: str
    parts: list[dict[str, Any]]
    #: `"platform"` when the platform wrote this turn rather than the person the
    #: role suggests — a Goal instruction, a compaction summary, a delegated
    #: child's report. Absent otherwise, which is every turn written before the
    #: field existed. Carried out to callers because that is the whole point of
    #: it: a transcript where the platform's words cannot be told from
    #: somebody's own misattributes them, and dropping the field here undid the
    #: distinction the store went to the trouble of keeping.
    author: str | None = None
    #: When somebody took this turn back. The row is still here on purpose —
    #: `list_session_messages` is the one read that does not filter withdrawn
    #: rows, because a transcript that silently dropped them would tell a
    #: reader the message was never said. That only works if the fact travels
    #: all the way out: a response model that stopped at `document()` would
    #: hand the console a message it cannot tell apart from a live one.
    withdrawn_at: datetime | None = None

    @classmethod
    def from_domain(cls, stored: StoredMessage) -> "SessionMessageResponse":
        return cls.model_validate(
            {**stored.message.document(), "withdrawn_at": stored.withdrawn_at}
        )


class QueueResponse(BaseModel):
    position: int
    status: str
    blocked_by_run_id: UUID | None = None
    head_status: str | None = None
    head_reason: dict[str, Any] | None = None
    available_actions: list[str] | None = None

    @model_serializer(mode="wrap")
    def omit_head_fields_unless_blocked(self, serializer: Any) -> dict[str, Any]:
        """Head/pending/terminal stay the short object existing clients already parse."""
        data = serializer(self)
        if data.get("status") != "session_blocked":
            for key in (
                "blocked_by_run_id",
                "head_status",
                "head_reason",
                "available_actions",
            ):
                data.pop(key, None)
        return data


class TreeNodeResponse(BaseModel):
    id: UUID
    status: str
    depth: int
    parent_run_id: UUID | None
    #: `root`, `child` or `retry` — why this Run shares the budget. Without
    #: it a retried Run reads as one the root delegated to.
    relation: str
    created_at: datetime
    finished_at: datetime | None


class RunTreeResponse(BaseModel):
    budget_root_run_id: UUID
    nodes: list[TreeNodeResponse]
    #: Carried once, for the tree. It is what every Run in the tree already
    #: reports as its own `budget` — the same `run_budget_scopes` row — and
    #: saying it here is what lets a console stop labelling a tree's spend as
    #: one Run's.
    budget: dict[str, Any]


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
    #: §13. `None` and `0` for the ordinary Run, which is most of them.
    parent_run_id: UUID | None
    depth: int
    #: ``[{"id", "status"}]`` — the Runs this one delegated, oldest first. The
    #: minimal task tree: enough for a console to show that this Run is one of
    #: several and to link to the others.
    children: list[dict[str, Any]]
    last_event_sequence: int
    queue: QueueResponse
    budget: dict[str, Any]
    available_actions: list[str]
    checkpoint_replay_safe: bool
    checkpoint_effect_status: str
    checkpoint_usage_quality: str | None
    failure_reason: str | None
    #: ``{"round", "outcome", "unmet", "preempted"}`` — which round the Run is
    #: on and what the platform decided about it. A status says a Run is
    #: still going; this says why. ``preempted`` (§12.1, v2.9.1) is true only
    #: when the round's own verdict said `continue` and the platform ended
    #: the Run anyway because a message arrived after it started — a
    #: `completed` Run that did not actually finish its own goal.
    goal: dict[str, Any]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_domain(cls, run: RunSnapshot) -> "RunResponse":
        return cls.model_validate(run.document())


class UsageByQualityResponse(BaseModel):
    cost_quality: str
    #: A decimal string, or null for "nothing here can be priced" — never a
    #: `0`. Same convention as `RunResponse`'s own `budget.consumed_cost`.
    consumed_cost: str | None
    cost_currency: str | None
    run_count: int
    consumed_model_calls: int
    consumed_tool_calls: int
    consumed_tokens: int
    consumed_execution_ms: int


class UsageSummaryResponse(BaseModel):
    """A workspace's usage, grouped by `cost_quality`.

    Deliberately no top-level cost field: the only place a cost figure
    appears is inside `by_cost_quality`, keyed by how far it can be trusted.
    The totals here are safe to blend across quality because none of them is
    money.
    """

    window: str
    by_cost_quality: list[UsageByQualityResponse]
    total_run_count: int
    total_model_calls: int
    total_tool_calls: int
    total_tokens: int
    total_execution_ms: int

    @classmethod
    def from_domain(cls, summary: WorkspaceUsageSummary) -> "UsageSummaryResponse":
        return cls.model_validate(summary.document())


def session_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])
    auth_dependency = resources.auth_service
    machines_dependency = resources.machine_identity_service
    runs_dependency = resources.run_coordination

    @router.get("", response_model=list[SessionResponse])
    async def list_sessions(  # pyright: ignore[reportUnusedFunction]
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        runs: Annotated[RunCoordination, Depends(runs_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        authorization: AuthorizationHeader = None,
    ) -> list[SessionResponse]:
        caller = await resolve_workspace_caller(
            auth,
            machines,
            session_token=session_token,
            authorization=authorization,
            csrf_token=None,
            workspace_header=selected_workspace,
            write=False,
            required_scope="runs.read",
        )
        try:
            sessions = await runs.list_sessions(caller.workspace_id, caller.actor)
        except RunCoordinationError as error:
            raise as_app_error(error) from error
        return [SessionResponse.from_domain(item) for item in sessions]

    @router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
    async def create_session(  # pyright: ignore[reportUnusedFunction]
        payload: CreateSessionRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        runs: Annotated[RunCoordination, Depends(runs_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
        authorization: AuthorizationHeader = None,
    ) -> SessionResponse:
        caller = await resolve_workspace_caller(
            auth,
            machines,
            session_token=session_token,
            authorization=authorization,
            csrf_token=csrf_token,
            workspace_header=selected_workspace,
            write=True,
            required_scope="runs.write",
        )
        try:
            created = await runs.create_session(
                caller.workspace_id,
                caller.actor,
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
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        runs: Annotated[RunCoordination, Depends(runs_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        authorization: AuthorizationHeader = None,
    ) -> SessionResponse:
        caller = await resolve_workspace_caller(
            auth,
            machines,
            session_token=session_token,
            authorization=authorization,
            csrf_token=None,
            workspace_header=selected_workspace,
            write=False,
            required_scope="runs.read",
        )
        try:
            found = await runs.get_session(caller.workspace_id, caller.actor, session_id)
        except RunCoordinationError as error:
            raise as_app_error(error) from error
        return SessionResponse.from_domain(found)

    @router.get("/{session_id}/messages", response_model=list[SessionMessageResponse])
    async def list_session_messages(  # pyright: ignore[reportUnusedFunction]
        session_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        runs: Annotated[RunCoordination, Depends(runs_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        authorization: AuthorizationHeader = None,
    ) -> list[SessionMessageResponse]:
        caller = await resolve_workspace_caller(
            auth,
            machines,
            session_token=session_token,
            authorization=authorization,
            csrf_token=None,
            workspace_header=selected_workspace,
            write=False,
            required_scope="runs.read",
        )
        try:
            messages = await runs.read_session_messages(
                caller.workspace_id, caller.actor, session_id, request.state.request_id
            )
        except RunCoordinationError as error:
            raise as_app_error(error) from error
        return [SessionMessageResponse.from_domain(item) for item in messages]

    return router


def run_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/runs", tags=["runs"])
    auth_dependency = resources.auth_service
    machines_dependency = resources.machine_identity_service
    runs_dependency = resources.run_coordination
    artifacts_dependency = resources.artifact_service

    async def _announce(workspace_id: UUID, run_id: UUID) -> None:
        """Tell Workers after the transaction that created work committed."""
        await resources.wake_up_notifier().publish(workspace_id, run_id)

    @router.get("", response_model=list[RunResponse])
    async def list_runs(  # pyright: ignore[reportUnusedFunction]
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        runs: Annotated[RunCoordination, Depends(runs_dependency, scope="function")],
        session_id: UUID | None = None,
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        authorization: AuthorizationHeader = None,
    ) -> list[RunResponse]:
        caller = await resolve_workspace_caller(
            auth,
            machines,
            session_token=session_token,
            authorization=authorization,
            csrf_token=None,
            workspace_header=selected_workspace,
            write=False,
            required_scope="runs.read",
        )
        try:
            found = await runs.list_runs(caller.workspace_id, caller.actor, session_id)
        except RunCoordinationError as error:
            raise as_app_error(error) from error
        return [RunResponse.from_domain(item) for item in found]

    @router.post("", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
    async def create_run(  # pyright: ignore[reportUnusedFunction]
        payload: CreateRunRequest,
        request: Request,
        response: Response,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        runs: Annotated[RunCoordination, Depends(runs_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
        idempotency_key: IdempotencyHeader = None,
        authorization: AuthorizationHeader = None,
    ) -> RunResponse:
        caller = await resolve_workspace_caller(
            auth,
            machines,
            session_token=session_token,
            authorization=authorization,
            csrf_token=csrf_token,
            workspace_header=selected_workspace,
            write=True,
            required_scope="runs.write",
        )
        try:
            accepted = await runs.submit_run(
                caller.workspace_id,
                caller.actor,
                payload.session_id,
                payload.input,
                idempotency_key,
                request.state.request_id,
            )
        except RunCoordinationError as error:
            raise as_app_error(error) from error
        _apply_acceptance_headers(response, accepted.replayed, accepted.run_id)
        if not accepted.replayed:
            await _announce(caller.workspace_id, accepted.run_id)
        return RunResponse.model_validate(accepted.document)

    @router.get("/{run_id}", response_model=RunResponse)
    async def get_run(  # pyright: ignore[reportUnusedFunction]
        run_id: UUID,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        runs: Annotated[RunCoordination, Depends(runs_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        authorization: AuthorizationHeader = None,
    ) -> RunResponse:
        caller = await resolve_workspace_caller(
            auth,
            machines,
            session_token=session_token,
            authorization=authorization,
            csrf_token=None,
            workspace_header=selected_workspace,
            write=False,
            required_scope="runs.read",
        )
        try:
            found = await runs.get_run(caller.workspace_id, caller.actor, run_id)
        except RunCoordinationError as error:
            raise as_app_error(error) from error
        return RunResponse.from_domain(found)

    @router.get("/{run_id}/tree", response_model=RunTreeResponse)
    async def get_run_tree(  # pyright: ignore[reportUnusedFunction]
        run_id: UUID,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        runs: Annotated[RunCoordination, Depends(runs_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        authorization: AuthorizationHeader = None,
    ) -> RunTreeResponse:
        """§952's 完整父子任务树, answerable from any Run in it.

        Its own route rather than more fields on `RunResponse`: a tree is the
        same answer for every node, and putting it on the Run would make one
        console page fetch it once per node it draws. `runs.read` because it
        reads Runs and nothing else.
        """
        caller = await resolve_workspace_caller(
            auth,
            machines,
            session_token=session_token,
            authorization=authorization,
            csrf_token=None,
            workspace_header=selected_workspace,
            write=False,
            required_scope="runs.read",
        )
        try:
            tree = await runs.run_tree(caller.workspace_id, caller.actor, run_id)
        except RunCoordinationError as error:
            raise as_app_error(error) from error
        return RunTreeResponse.model_validate(tree.document())

    @router.get("/{run_id}/artifacts", response_model=list[ArtifactResponse])
    async def list_run_artifacts(  # pyright: ignore[reportUnusedFunction]
        run_id: UUID,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        runs: Annotated[RunCoordination, Depends(runs_dependency, scope="function")],
        artifacts: Annotated[
            ArtifactService, Depends(artifacts_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        authorization: AuthorizationHeader = None,
    ) -> list[ArtifactResponse]:
        caller = await resolve_workspace_caller(
            auth,
            machines,
            session_token=session_token,
            authorization=authorization,
            csrf_token=None,
            workspace_header=selected_workspace,
            write=False,
            required_scope="runs.read",
        )
        try:
            await runs.get_run(caller.workspace_id, caller.actor, run_id)
            found = await artifacts.list_for_run(
                caller.workspace_id, caller.actor, run_id
            )
        except RunCoordinationError as error:
            raise as_app_error(error) from error
        except ArtifactForbidden as error:
            raise forbidden() from error
        return [
            ArtifactResponse(
                id=item.id,
                run_id=item.run_id,
                session_id=item.session_id,
                filename=item.filename,
                media_type=item.media_type,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
                truncated=item.truncated,
                expires_at=item.expires_at,
            )
            for item in found
        ]

    async def _control(
        run_id: UUID,
        payload: ControlRunRequest,
        request: Request,
        auth: AuthService,
        machines: MachineIdentityService,
        runs: RunCoordination,
        signal: RunSignal,
        session_token: str | None,
        csrf_token: str | None,
        selected_workspace: str | None,
        authorization: str | None,
    ) -> RunResponse:
        caller = await resolve_workspace_caller(
            auth,
            machines,
            session_token=session_token,
            authorization=authorization,
            csrf_token=csrf_token,
            workspace_header=selected_workspace,
            write=True,
            required_scope="runs.control",
        )
        try:
            updated = await runs.control_run(
                caller.workspace_id,
                caller.actor,
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
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        runs: Annotated[RunCoordination, Depends(runs_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
        idempotency_key: IdempotencyHeader = None,
        authorization: AuthorizationHeader = None,
    ) -> RunResponse:
        caller = await resolve_workspace_caller(
            auth,
            machines,
            session_token=session_token,
            authorization=authorization,
            csrf_token=csrf_token,
            workspace_header=selected_workspace,
            write=True,
            required_scope="runs.control",
        )
        try:
            accepted = await runs.retry_run(
                caller.workspace_id,
                caller.actor,
                run_id,
                idempotency_key,
                request.state.request_id,
            )
        except RunCoordinationError as error:
            raise as_app_error(error) from error
        _apply_acceptance_headers(response, accepted.replayed, accepted.run_id)
        if not accepted.replayed:
            await _announce(caller.workspace_id, accepted.run_id)
        return RunResponse.model_validate(accepted.document)

    @router.post("/{run_id}/pause", response_model=RunResponse)
    async def pause_run(  # pyright: ignore[reportUnusedFunction]
        run_id: UUID,
        payload: ControlRunRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        runs: Annotated[RunCoordination, Depends(runs_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
        authorization: AuthorizationHeader = None,
    ) -> RunResponse:
        return await _control(
            run_id,
            payload,
            request,
            auth,
            machines,
            runs,
            RunSignal.PAUSE_REQUESTED,
            session_token,
            csrf_token,
            selected_workspace,
            authorization,
        )

    @router.post("/{run_id}/resume", response_model=RunResponse)
    async def resume_run(  # pyright: ignore[reportUnusedFunction]
        run_id: UUID,
        payload: ControlRunRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        runs: Annotated[RunCoordination, Depends(runs_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
        authorization: AuthorizationHeader = None,
    ) -> RunResponse:
        return await _control(
            run_id,
            payload,
            request,
            auth,
            machines,
            runs,
            RunSignal.RESUME_REQUESTED,
            session_token,
            csrf_token,
            selected_workspace,
            authorization,
        )

    @router.post("/{run_id}/budget", response_model=RunResponse)
    async def widen_run_budget(  # pyright: ignore[reportUnusedFunction]
        run_id: UUID,
        payload: WidenBudgetRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        runs: Annotated[RunCoordination, Depends(runs_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
        authorization: AuthorizationHeader = None,
    ) -> RunResponse:
        caller = await resolve_workspace_caller(
            auth,
            machines,
            session_token=session_token,
            authorization=authorization,
            csrf_token=csrf_token,
            workspace_header=selected_workspace,
            write=True,
            required_scope="runs.control",
        )
        try:
            updated = await runs.widen_budget(
                caller.workspace_id,
                caller.actor,
                run_id,
                payload.expected_state_version,
                payload.max_model_calls,
                request.state.request_id,
            )
        except RunCoordinationError as error:
            raise as_app_error(error) from error
        return RunResponse.from_domain(updated)

    @router.post("/{run_id}/cancel", response_model=RunResponse)
    async def cancel_run(  # pyright: ignore[reportUnusedFunction]
        run_id: UUID,
        payload: ControlRunRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        runs: Annotated[RunCoordination, Depends(runs_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
        authorization: AuthorizationHeader = None,
    ) -> RunResponse:
        return await _control(
            run_id,
            payload,
            request,
            auth,
            machines,
            runs,
            RunSignal.CANCEL_REQUESTED,
            session_token,
            csrf_token,
            selected_workspace,
            authorization,
        )

    return router


def usage_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/usage", tags=["usage"])
    auth_dependency = resources.auth_service
    machines_dependency = resources.machine_identity_service
    runs_dependency = resources.run_coordination

    @router.get("", response_model=UsageSummaryResponse)
    async def get_usage_summary(  # pyright: ignore[reportUnusedFunction]
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        runs: Annotated[RunCoordination, Depends(runs_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        authorization: AuthorizationHeader = None,
    ) -> UsageSummaryResponse:
        caller = await resolve_workspace_caller(
            auth,
            machines,
            session_token=session_token,
            authorization=authorization,
            csrf_token=None,
            workspace_header=selected_workspace,
            write=False,
            required_scope="runs.read",
        )
        try:
            summary = await runs.usage_summary(caller.workspace_id, caller.actor)
        except RunCoordinationError as error:
            raise as_app_error(error) from error
        return UsageSummaryResponse.from_domain(summary)

    return router


def _apply_acceptance_headers(response: Response, replayed: bool, run_id: UUID) -> None:
    if replayed:
        response.status_code = status.HTTP_200_OK
        response.headers[REPLAYED_HEADER] = "true"
        return
    response.status_code = status.HTTP_201_CREATED
    response.headers["Location"] = f"/api/v1/runs/{run_id}"
