"""The outbound scope's HTTP face: two levels, two different powers.

`/api/v1/outbound-scopes/platform` is a platform administrator's list, readable
by any workspace administrator because they are choosing inside it.
`/api/v1/outbound-scopes/workspace` is one workspace's own, and every entry it
accepts has to be contained by the platform's.

There is no endpoint for the Agent level: it is part of the AgentSpec and is
published with the version, like `tools` and `skills`.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, Request, status
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
from tiny_hermes.outbound.application.service import (
    ForbiddenScopeAction,
    InvalidScopeEntry,
    OutboundScopes,
    ScopeEntryManaged,
    ScopeEntryNotFound,
    ScopeEntryOutsidePlatform,
    ScopeEntryRecord,
)
from tiny_hermes.shared.errors import AppError
from tiny_hermes.tenancy.domain.models import Actor

WorkspaceHeader = Annotated[str | None, Header(alias="X-Workspace-Id")]
CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token")]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


class ApproveEntryRequest(BaseModel):
    #: A host, a one-level wildcard, or a network. Never a URL and never a
    #: port: a scope approves a target, and the port belongs to the request.
    entry: str = Field(min_length=1, max_length=255)
    note: str | None = Field(default=None, max_length=255)


class ScopeEntryResponse(BaseModel):
    id: UUID
    level: str
    workspace_id: UUID | None
    entry: str
    note: str | None
    #: True when a model endpoint owns this entry. The console shows no remove
    #: button for one — it appears and disappears with the endpoint, so a
    #: control that fought that would only ever lose.
    managed: bool
    created_at: datetime

    @classmethod
    def from_domain(cls, record: ScopeEntryRecord) -> "ScopeEntryResponse":
        return cls(
            id=record.id,
            level=record.level,
            workspace_id=record.workspace_id,
            entry=record.entry,
            note=record.note,
            managed=record.managed,
            created_at=record.created_at,
        )


def outbound_scope_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/outbound-scopes", tags=["outbound"])
    auth_dependency = resources.auth_service
    scopes_dependency = resources.outbound_scopes

    @router.get("/platform", response_model=list[ScopeEntryResponse])
    async def list_platform(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        scopes: Annotated[OutboundScopes, Depends(scopes_dependency, scope="function")],
        session_token: SessionCookie = None,
    ) -> list[ScopeEntryResponse]:
        user = await authenticate_browser_user(auth, session_token)
        try:
            listed = await scopes.list_platform(_actor(user), request.state.request_id)
        except ForbiddenScopeAction as error:
            raise forbidden() from error
        return [ScopeEntryResponse.from_domain(record) for record in listed]

    @router.post(
        "/platform",
        response_model=ScopeEntryResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def approve_platform(  # pyright: ignore[reportUnusedFunction]
        payload: ApproveEntryRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        scopes: Annotated[OutboundScopes, Depends(scopes_dependency, scope="function")],
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> ScopeEntryResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        try:
            record = await scopes.approve_platform(
                _actor(user), payload.entry, payload.note, request.state.request_id
            )
        except ForbiddenScopeAction as error:
            raise forbidden() from error
        except InvalidScopeEntry as error:
            raise _invalid(error) from error
        return ScopeEntryResponse.from_domain(record)

    @router.get("/workspace", response_model=list[ScopeEntryResponse])
    async def list_workspace(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        scopes: Annotated[OutboundScopes, Depends(scopes_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> list[ScopeEntryResponse]:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            listed = await scopes.list_workspace(
                _actor(user), workspace_id, request.state.request_id
            )
        except ForbiddenScopeAction as error:
            raise forbidden() from error
        return [ScopeEntryResponse.from_domain(record) for record in listed]

    @router.post(
        "/workspace",
        response_model=ScopeEntryResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def approve_workspace(  # pyright: ignore[reportUnusedFunction]
        payload: ApproveEntryRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        scopes: Annotated[OutboundScopes, Depends(scopes_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> ScopeEntryResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            record = await scopes.approve_workspace(
                _actor(user),
                workspace_id,
                payload.entry,
                payload.note,
                request.state.request_id,
            )
        except ForbiddenScopeAction as error:
            raise forbidden() from error
        except InvalidScopeEntry as error:
            raise _invalid(error) from error
        except ScopeEntryOutsidePlatform as error:
            raise _outside(error) from error
        return ScopeEntryResponse.from_domain(record)

    @router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def revoke(  # pyright: ignore[reportUnusedFunction]
        entry_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        scopes: Annotated[OutboundScopes, Depends(scopes_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> None:
        user = await verify_browser_write(auth, session_token, csrf_token)
        # Optional: a platform entry is not addressed through a workspace, and
        # requiring the header would make removing one depend on which
        # workspace the console happened to be showing.
        workspace_id = (
            None if selected_workspace is None else require_workspace_id(selected_workspace)
        )
        try:
            await scopes.revoke(
                _actor(user), entry_id, request.state.request_id, workspace_id
            )
        except ForbiddenScopeAction as error:
            raise forbidden() from error
        except ScopeEntryNotFound as error:
            raise _not_found() from error
        except ScopeEntryManaged as error:
            raise _managed() from error

    return router


def _actor(user: AuthenticatedUser) -> Actor:
    return Actor(user.id, user.is_platform_admin)


def _invalid(error: InvalidScopeEntry) -> AppError:
    return AppError(
        code="invalid_outbound_entry",
        title="Invalid outbound entry",
        status=422,
        detail=error.reason,
    )


def _outside(error: ScopeEntryOutsidePlatform) -> AppError:
    return AppError(
        code="outbound_entry_outside_platform",
        title="Outside the approved range",
        status=422,
        detail=(
            f"{error.entry} is not inside anything a platform administrator has "
            "approved. Ask for the range to be opened, or choose a target "
            "inside it."
        ),
    )


def _not_found() -> AppError:
    return AppError(
        code="outbound_entry_not_found",
        title="Entry not found",
        status=404,
        detail="No outbound entry by that identifier is available here.",
    )


def _managed() -> AppError:
    return AppError(
        code="outbound_entry_managed",
        title="Entry belongs to a model endpoint",
        status=409,
        detail=(
            "This target is approved because a model endpoint names it. "
            "Disable the endpoint to take the approval away."
        ),
    )
