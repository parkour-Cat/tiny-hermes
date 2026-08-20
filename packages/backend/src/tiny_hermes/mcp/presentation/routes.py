"""The MCP catalog's HTTP face.

Shaped after the HTTP tool catalog's, with one route that has no counterpart
there: `POST /{server_id}/refresh` reads the server again. There is no upload
endpoint, because there is no document to upload — a snapshot is something this
platform goes and gets.

Refreshing an unchanged server answers 200 rather than 201 and adds no version.
The point of a version is that somebody reviewed it, and a snapshot identical
to the last one has nothing new to review.
"""

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
from tiny_hermes.mcp.application.service import (
    ForbiddenMcpAction,
    HostOutsideWorkspaceScope,
    InvalidMcpServerName,
    InvalidMcpUrl,
    McpCapabilitiesRefused,
    McpCatalog,
    McpServerNameTaken,
    McpUnreachable,
    UnknownMcpServer,
    UnknownMcpServerVersion,
)
from tiny_hermes.mcp.domain.models import McpServer, McpServerVersion
from tiny_hermes.shared.errors import AppError
from tiny_hermes.tenancy.domain.models import Actor

WorkspaceHeader = Annotated[str | None, Header(alias="X-Workspace-Id")]
CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token")]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


class RegisterServerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=48)
    url: str = Field(min_length=1, max_length=2048)
    credential_ref: str | None = Field(default=None, max_length=255)


class McpServerResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    url: str
    credential_ref: str | None
    current_version_id: UUID | None
    #: When this platform last got an answer out of the server. "Registered"
    #: and "reachable" are different facts and the list shows both.
    last_validated_at: datetime | None
    created_at: datetime
    updated_at: datetime


class McpToolResponse(BaseModel):
    name: str
    description: str | None
    input_schema: dict[str, Any]


class McpServerVersionResponse(BaseModel):
    id: UUID
    mcp_server_id: UUID
    version_number: int
    content_hash: str
    tools: list[McpToolResponse]
    status: str
    bindable: bool
    created_at: datetime


def mcp_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/mcp-servers", tags=["mcp"])
    auth_dependency = resources.auth_service
    catalog_dependency = resources.mcp_catalog

    @router.get("", response_model=list[McpServerResponse])
    async def list_servers(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[McpCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> list[McpServerResponse]:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            listed = await catalog.list_servers(
                _actor(user), workspace_id, request.state.request_id
            )
        except ForbiddenMcpAction as error:
            raise forbidden() from error
        return [_server(server) for server in listed]

    @router.post(
        "", response_model=McpServerResponse, status_code=status.HTTP_201_CREATED
    )
    async def register_server(  # pyright: ignore[reportUnusedFunction]
        payload: RegisterServerRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[McpCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> McpServerResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            server, _ = await catalog.register(
                _actor(user),
                workspace_id,
                name=payload.name,
                url=payload.url,
                credential_ref=payload.credential_ref,
                request_id=request.state.request_id,
            )
        except ForbiddenMcpAction as error:
            raise forbidden() from error
        except McpServerNameTaken as error:
            raise AppError(
                code="mcp_server_name_taken",
                title="Server name taken",
                status=409,
                detail="A server by that name already exists in this workspace.",
            ) from error
        except InvalidMcpServerName as error:
            raise AppError(
                code="invalid_mcp_server_name",
                title="Invalid server name",
                status=422,
                detail=(
                    "A server name is lowercase letters, digits and single "
                    "hyphens: it becomes part of a name the model types back."
                ),
            ) from error
        except HostOutsideWorkspaceScope as error:
            raise _outside_scope(error) from error
        except InvalidMcpUrl as error:
            raise AppError(
                code="invalid_mcp_url",
                title="Invalid server URL",
                status=422,
                detail=error.reason,
            ) from error
        except McpUnreachable as error:
            raise _unreachable(error) from error
        except McpCapabilitiesRefused as error:
            raise _refused(error) from error
        return _server(server)

    @router.get("/{server_id}/versions", response_model=list[McpServerVersionResponse])
    async def list_versions(  # pyright: ignore[reportUnusedFunction]
        server_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[McpCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> list[McpServerVersionResponse]:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            versions = await catalog.list_versions(
                _actor(user), workspace_id, server_id, request.state.request_id
            )
        except ForbiddenMcpAction as error:
            raise forbidden() from error
        except UnknownMcpServer as error:
            raise _server_not_found() from error
        return [_version(version) for version in versions]

    @router.post(
        "/{server_id}/refresh",
        response_model=McpServerVersionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def refresh(  # pyright: ignore[reportUnusedFunction]
        server_id: UUID,
        request: Request,
        response: Response,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[McpCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> McpServerVersionResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            result = await catalog.refresh(
                _actor(user), workspace_id, server_id, request.state.request_id
            )
        except ForbiddenMcpAction as error:
            raise forbidden() from error
        except UnknownMcpServer as error:
            raise _server_not_found() from error
        except McpUnreachable as error:
            raise _unreachable(error) from error
        except McpCapabilitiesRefused as error:
            raise _refused(error) from error
        if not result.created:
            # Nothing new to review.
            response.status_code = status.HTTP_200_OK
        return _version(result.version)

    @router.post(
        "/{server_id}/versions/{version_id}/withdraw",
        response_model=McpServerVersionResponse,
    )
    async def withdraw_version(  # pyright: ignore[reportUnusedFunction]
        server_id: UUID,
        version_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[McpCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> McpServerVersionResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            withdrawn = await catalog.withdraw_version(
                _actor(user),
                workspace_id,
                server_id,
                version_id,
                request.state.request_id,
            )
        except ForbiddenMcpAction as error:
            raise forbidden() from error
        except UnknownMcpServer as error:
            raise _server_not_found() from error
        except UnknownMcpServerVersion as error:
            raise AppError(
                code="mcp_server_version_not_found",
                title="Version not found",
                status=404,
                detail="No version by that identifier belongs to this server.",
            ) from error
        return _version(withdrawn)

    return router


def _actor(user: AuthenticatedUser) -> Actor:
    return Actor(user.id, user.is_platform_admin)


def _server(server: McpServer) -> McpServerResponse:
    return McpServerResponse(
        id=server.id,
        workspace_id=server.workspace_id,
        name=server.name,
        url=server.url,
        credential_ref=server.credential_ref,
        current_version_id=server.current_version_id,
        last_validated_at=server.last_validated_at,
        created_at=server.created_at,
        updated_at=server.updated_at,
    )


def _version(version: McpServerVersion) -> McpServerVersionResponse:
    return McpServerVersionResponse(
        id=version.id,
        mcp_server_id=version.mcp_server_id,
        version_number=version.version_number,
        content_hash=version.content_hash,
        tools=[
            McpToolResponse(
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema,
            )
            for tool in version.tools
        ],
        status=version.status.value,
        bindable=version.bindable,
        created_at=version.created_at,
    )


def _server_not_found() -> AppError:
    return AppError(
        code="mcp_server_not_found",
        title="Server not found",
        status=404,
        detail="No server by that identifier is available from this workspace.",
    )


def _unreachable(error: McpUnreachable) -> AppError:
    """502 rather than 422: nothing about the request was wrong.

    Named apart from a refused answer because only one of the two is worth
    trying again — a server that was down may be up in a minute, and a server
    that advertised two tools with one name will do it again.
    """
    return AppError(
        code="mcp_server_unreachable",
        title="Server unreachable",
        status=502,
        detail=error.reason,
    )


def _refused(error: McpCapabilitiesRefused) -> AppError:
    return AppError(
        code="invalid_mcp_capabilities",
        title="Unusable capability list",
        status=422,
        detail=error.reason,
    )


def _outside_scope(error: HostOutsideWorkspaceScope) -> AppError:
    return AppError(
        code="host_outside_workspace_scope",
        title="Host not approved",
        status=422,
        detail=(
            f"{error.host} is not in this workspace's outbound scope. "
            "A workspace administrator approves it under Outbound scope first."
        ),
    )
