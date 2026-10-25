"""The console's face of published sites (§1.26, v2.26).

Any member lists and revokes; a developer or administrator extends; only an
administrator opens a site to anyone with the link. `/open` is the members'
door: the console's session is checked here, on the console's origin, and a
one-minute ticket carries the answer to the site's.
"""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, Request, status
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel

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
from tiny_hermes.shared.errors import AppError
from tiny_hermes.sites.application.publisher import SiteView
from tiny_hermes.sites.domain.site import sign
from tiny_hermes.tenancy.application.workspace_service import Forbidden, WorkspaceService
from tiny_hermes.tenancy.domain.models import Actor

WorkspaceHeader = Annotated[str | None, Header(alias="X-Workspace-Id")]
CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token")]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]

TICKET_SECONDS = 60
EXTENDERS = frozenset({"workspace_admin", "developer", "platform_admin"})
OPENERS = frozenset({"workspace_admin", "platform_admin"})


class SiteResponse(BaseModel):
    id: UUID
    run_id: UUID
    title: str | None
    root: str
    visibility: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    #: Neither revoked nor expired. The link itself is never listed.
    active: bool

    @classmethod
    def from_view(cls, view: SiteView, now: datetime) -> "SiteResponse":
        return cls(
            id=view.id,
            run_id=view.run_id,
            title=view.title,
            root=view.root,
            visibility=view.visibility,
            created_at=view.created_at,
            expires_at=view.expires_at,
            revoked_at=view.revoked_at,
            active=view.revoked_at is None and view.expires_at > now,
        )


class ExtendedResponse(BaseModel):
    expires_at: datetime


class VisibilityRequest(BaseModel):
    visibility: Literal["members", "link"]


class VisibilityResponse(BaseModel):
    visibility: str


def site_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/sites", tags=["sites"])
    auth_dependency = resources.auth_service
    workspace_dependency = resources.workspace_service

    async def role_of(
        workspaces: WorkspaceService, user: AuthenticatedUser, workspace: UUID
    ) -> str:
        try:
            return await workspaces.my_role(_actor(user), workspace)
        except Forbidden as error:
            raise forbidden() from error

    @router.get("", response_model=list[SiteResponse])
    async def list_sites(  # pyright: ignore[reportUnusedFunction]
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        workspaces: Annotated[WorkspaceService, Depends(workspace_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> list[SiteResponse]:
        user = await authenticate_browser_user(auth, session_token)
        workspace = require_workspace_id(selected_workspace)
        await role_of(workspaces, user, workspace)
        now = datetime.now(UTC)
        return [
            SiteResponse.from_view(view, now)
            for view in await resources.site_publisher().list(workspace)
        ]

    @router.post("/{site_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
    async def revoke_site(  # pyright: ignore[reportUnusedFunction]
        site_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        workspaces: Annotated[WorkspaceService, Depends(workspace_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> Response:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace = require_workspace_id(selected_workspace)
        await role_of(workspaces, user, workspace)
        revoked = await resources.site_publisher().revoke(
            workspace, site_id, actor_id=user.id, request_id=request.state.request_id
        )
        if not revoked:
            raise _not_found()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/{site_id}/extend", response_model=ExtendedResponse)
    async def extend_site(  # pyright: ignore[reportUnusedFunction]
        site_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        workspaces: Annotated[WorkspaceService, Depends(workspace_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> ExtendedResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace = require_workspace_id(selected_workspace)
        if await role_of(workspaces, user, workspace) not in EXTENDERS:
            raise forbidden()
        expires = await resources.site_publisher().extend(
            workspace, site_id, actor_id=user.id, request_id=request.state.request_id
        )
        if expires is None:
            raise _not_found()
        return ExtendedResponse(expires_at=expires)

    @router.put("/{site_id}/visibility", response_model=VisibilityResponse)
    async def set_visibility(  # pyright: ignore[reportUnusedFunction]
        site_id: UUID,
        payload: VisibilityRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        workspaces: Annotated[WorkspaceService, Depends(workspace_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> VisibilityResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace = require_workspace_id(selected_workspace)
        if await role_of(workspaces, user, workspace) not in OPENERS:
            raise forbidden()
        changed = await resources.site_publisher().set_visibility(
            workspace,
            site_id,
            payload.visibility,
            actor_id=user.id,
            request_id=request.state.request_id,
        )
        if not changed:
            raise _not_found()
        return VisibilityResponse(visibility=payload.visibility)

    @router.get("/open")
    async def open_site(  # pyright: ignore[reportUnusedFunction]
        t: str,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        workspaces: Annotated[WorkspaceService, Depends(workspace_dependency, scope="function")],
        session_token: SessionCookie = None,
    ) -> RedirectResponse:
        publisher = resources.site_publisher()
        settings = publisher.settings
        if not settings.public_url or not settings.signing_secret:
            raise _not_found()
        site = await publisher.resolve(t)
        if site is None:
            raise _not_found()
        base = f"{settings.public_url.rstrip('/')}/s/{t}/"
        if site.visibility == "link":
            return RedirectResponse(base, status_code=status.HTTP_302_FOUND)
        user = await authenticate_browser_user(auth, session_token)
        await role_of(workspaces, user, site.workspace_id)
        ticket = sign(
            settings.signing_secret,
            "ticket",
            {"p": str(site.id), "u": str(user.id), "n": secrets.token_hex(16)},
            datetime.now(UTC) + timedelta(seconds=TICKET_SECONDS),
        )
        return RedirectResponse(
            f"{base}__auth?ticket={ticket}", status_code=status.HTTP_302_FOUND
        )

    return router


def _actor(user: AuthenticatedUser) -> Actor:
    return Actor(user.id, user.is_platform_admin)


def _not_found() -> AppError:
    return AppError(
        code="site_not_found",
        title="Site not found",
        status=404,
        detail="No such site, or it has been revoked or has expired.",
    )
