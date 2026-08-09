from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, Request, status
from pydantic import BaseModel, Field

from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.identity.application.auth_service import AuthService, InvalidCredentials
from tiny_hermes.identity.domain.models import AuthenticatedUser
from tiny_hermes.identity.presentation.routes import SESSION_COOKIE
from tiny_hermes.shared.errors import AppError
from tiny_hermes.tenancy.application.workspace_service import (
    Forbidden,
    InvalidWorkspaceName,
    WorkspaceService,
)
from tiny_hermes.tenancy.domain.models import Actor, Workspace, WorkspaceMember


class CreateWorkspaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class WorkspaceResponse(BaseModel):
    id: UUID
    name: str
    status: str

    @classmethod
    def from_domain(cls, workspace: Workspace) -> "WorkspaceResponse":
        return cls(id=workspace.id, name=workspace.name, status=workspace.status)


class WorkspaceMemberResponse(BaseModel):
    user_id: UUID
    display_name: str
    role: str

    @classmethod
    def from_domain(cls, member: WorkspaceMember) -> "WorkspaceMemberResponse":
        return cls(user_id=member.user_id, display_name=member.display_name, role=member.role.value)


def workspace_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])
    auth_dependency = resources.auth_service
    workspace_dependency = resources.workspace_service

    @router.get("", response_model=list[WorkspaceResponse])
    async def list_workspaces(  # pyright: ignore[reportUnusedFunction]
        auth: Annotated[AuthService, Depends(auth_dependency)],
        workspaces: Annotated[WorkspaceService, Depends(workspace_dependency)],
        session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> list[WorkspaceResponse]:
        user = await _authenticate(auth, session_token)
        values = await workspaces.list_workspaces(_actor(user))
        return [WorkspaceResponse.from_domain(value) for value in values]

    @router.post("", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
    async def create_workspace(  # pyright: ignore[reportUnusedFunction]
        payload: CreateWorkspaceRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency)],
        workspaces: Annotated[WorkspaceService, Depends(workspace_dependency)],
        session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> WorkspaceResponse:
        user = await _verify_write(auth, session_token, csrf_token)
        try:
            workspace = await workspaces.create_workspace(
                _actor(user), payload.name, request.state.request_id
            )
        except Forbidden as error:
            raise _forbidden() from error
        except InvalidWorkspaceName as error:
            raise AppError(
                code="invalid_workspace_name",
                title="Invalid workspace name",
                status=422,
                detail="The workspace name is invalid.",
            ) from error
        return WorkspaceResponse.from_domain(workspace)

    @router.get("/{workspace_id}/members", response_model=list[WorkspaceMemberResponse])
    async def list_members(  # pyright: ignore[reportUnusedFunction]
        workspace_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency)],
        workspaces: Annotated[WorkspaceService, Depends(workspace_dependency)],
        selected_workspace: Annotated[str | None, Header(alias="X-Workspace-Id")] = None,
        session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> list[WorkspaceMemberResponse]:
        user = await _authenticate(auth, session_token)
        if selected_workspace is None:
            raise AppError(
                code="workspace_required",
                title="Workspace required",
                status=400,
                detail="X-Workspace-Id is required for workspace-scoped requests.",
            )
        try:
            selected_id = UUID(selected_workspace)
        except ValueError as error:
            raise AppError(
                code="invalid_workspace_id",
                title="Invalid workspace identifier",
                status=400,
                detail="X-Workspace-Id must be a UUID.",
            ) from error
        if selected_id != workspace_id:
            raise AppError(
                code="workspace_scope_mismatch",
                title="Workspace scope mismatch",
                status=403,
                detail="The selected workspace does not match the requested resource.",
            )
        try:
            members = await workspaces.list_members(
                _actor(user), workspace_id, request.state.request_id
            )
        except Forbidden as error:
            raise _forbidden() from error
        return [WorkspaceMemberResponse.from_domain(member) for member in members]

    return router


def _actor(user: AuthenticatedUser) -> Actor:
    return Actor(user.id, user.is_platform_admin)


async def _authenticate(auth: AuthService, session_token: str | None) -> AuthenticatedUser:
    if session_token is None:
        raise _unauthenticated()
    try:
        return await auth.authenticate(session_token)
    except InvalidCredentials as error:
        raise _unauthenticated() from error


async def _verify_write(
    auth: AuthService, session_token: str | None, csrf_token: str | None
) -> AuthenticatedUser:
    if session_token is None:
        raise _unauthenticated()
    if csrf_token is None:
        raise _csrf_failed()
    try:
        return await auth.verify_csrf(session_token, csrf_token)
    except InvalidCredentials as error:
        raise _csrf_failed() from error


def _unauthenticated() -> AppError:
    return AppError(
        code="unauthenticated",
        title="Authentication required",
        status=401,
        detail="A valid browser session is required.",
    )


def _csrf_failed() -> AppError:
    return AppError(
        code="csrf_failed",
        title="Request verification failed",
        status=403,
        detail="The request verification token is missing or invalid.",
    )


def _forbidden() -> AppError:
    return AppError(
        code="forbidden",
        title="Forbidden",
        status=403,
        detail="The current user cannot perform this workspace action.",
    )
