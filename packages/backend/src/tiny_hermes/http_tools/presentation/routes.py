"""The HTTP tool catalog's own HTTP face.

Shaped after `skills/presentation/routes.py`, including the one status code
worth knowing: `POST /{tool_id}/versions` answers 201 when a version was
created and 200 when the same document already was one. Re-uploading an
unchanged export is not a publication.

No route here returns a credential, and none accepts one. `credential_ref`
names a Secret; the value is read where the call is made and has never been in
this module.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, Request, Response, status
from pydantic import BaseModel, Field

from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.http_tools.application.service import (
    ForbiddenHttpToolAction,
    HostOutsideWorkspaceScope,
    HttpToolCatalog,
    HttpToolNameTaken,
    InvalidBaseUrl,
    InvalidHttpToolName,
    InvalidOpenApiDocument,
    UnknownHttpTool,
    UnknownHttpToolVersion,
)
from tiny_hermes.http_tools.domain.models import HttpTool, HttpToolVersion
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
from tiny_hermes.tenancy.domain.models import Actor
from tiny_hermes.tools.domain.openapi import MAX_DOCUMENT_BYTES

WorkspaceHeader = Annotated[str | None, Header(alias="X-Workspace-Id")]
CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token")]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


class RegisterHttpToolRequest(BaseModel):
    name: str = Field(min_length=1, max_length=48)
    #: Where the requests go, decided here rather than read from the document's
    #: `servers` — see `HttpTool.base_url`.
    base_url: str = Field(min_length=1, max_length=2048)
    #: Bounded here as characters and again in the domain as UTF-8 bytes. This
    #: one stops a body large enough to be worth parsing from being parsed.
    document: str = Field(min_length=1, max_length=MAX_DOCUMENT_BYTES)
    credential_ref: str | None = Field(default=None, max_length=255)


class AddVersionRequest(BaseModel):
    document: str = Field(min_length=1, max_length=MAX_DOCUMENT_BYTES)


class HttpToolResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    base_url: str
    credential_ref: str | None
    current_version_id: UUID | None
    created_at: datetime
    updated_at: datetime


class OperationResponse(BaseModel):
    operation_id: str
    method: str
    path: str
    summary: str | None
    #: What the console shows beside a write, and what makes the approval
    #: requirement visible before somebody binds it rather than after.
    read_only: bool


class HttpToolVersionResponse(BaseModel):
    id: UUID
    http_tool_id: UUID
    version_number: int
    content_hash: str
    title: str
    document_version: str
    operations: list[OperationResponse]
    status: str
    bindable: bool
    created_at: datetime


def http_tool_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/http-tools", tags=["http-tools"])
    auth_dependency = resources.auth_service
    catalog_dependency = resources.http_tool_catalog

    @router.get("", response_model=list[HttpToolResponse])
    async def list_tools(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[
            HttpToolCatalog, Depends(catalog_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> list[HttpToolResponse]:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            listed = await catalog.list_tools(
                _actor(user), workspace_id, request.state.request_id
            )
        except ForbiddenHttpToolAction as error:
            raise forbidden() from error
        return [_tool(tool) for tool in listed]

    @router.post(
        "", response_model=HttpToolResponse, status_code=status.HTTP_201_CREATED
    )
    async def register_tool(  # pyright: ignore[reportUnusedFunction]
        payload: RegisterHttpToolRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[
            HttpToolCatalog, Depends(catalog_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> HttpToolResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            tool, _ = await catalog.register(
                _actor(user),
                workspace_id,
                name=payload.name,
                base_url=payload.base_url,
                document=payload.document,
                credential_ref=payload.credential_ref,
                request_id=request.state.request_id,
            )
        except ForbiddenHttpToolAction as error:
            raise forbidden() from error
        except HttpToolNameTaken as error:
            raise AppError(
                code="http_tool_name_taken",
                title="Tool name taken",
                status=409,
                detail="A tool by that name already exists in this workspace.",
            ) from error
        except InvalidHttpToolName as error:
            raise AppError(
                code="invalid_http_tool_name",
                title="Invalid tool name",
                status=422,
                detail=(
                    "A tool name is lowercase letters, digits and single "
                    "hyphens: it becomes part of a name the model types back."
                ),
            ) from error
        except HostOutsideWorkspaceScope as error:
            raise _outside_scope(error) from error
        except InvalidBaseUrl as error:
            raise AppError(
                code="invalid_base_url",
                title="Invalid base URL",
                status=422,
                detail=error.reason,
            ) from error
        except InvalidOpenApiDocument as error:
            raise _invalid_document(error) from error
        return _tool(tool)

    @router.get("/{tool_id}/versions", response_model=list[HttpToolVersionResponse])
    async def list_versions(  # pyright: ignore[reportUnusedFunction]
        tool_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[
            HttpToolCatalog, Depends(catalog_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> list[HttpToolVersionResponse]:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            versions = await catalog.list_versions(
                _actor(user), workspace_id, tool_id, request.state.request_id
            )
        except ForbiddenHttpToolAction as error:
            raise forbidden() from error
        except UnknownHttpTool as error:
            raise _tool_not_found() from error
        return [_version(version) for version in versions]

    @router.post(
        "/{tool_id}/versions",
        response_model=HttpToolVersionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def add_version(  # pyright: ignore[reportUnusedFunction]
        tool_id: UUID,
        payload: AddVersionRequest,
        request: Request,
        response: Response,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[
            HttpToolCatalog, Depends(catalog_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> HttpToolVersionResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            result = await catalog.add_version(
                _actor(user),
                workspace_id,
                tool_id,
                payload.document,
                request.state.request_id,
            )
        except ForbiddenHttpToolAction as error:
            raise forbidden() from error
        except UnknownHttpTool as error:
            raise _tool_not_found() from error
        except InvalidOpenApiDocument as error:
            raise _invalid_document(error) from error
        if not result.created:
            # The same document is the same version. 200 rather than 201 says
            # so without the caller having to compare hashes.
            response.status_code = status.HTTP_200_OK
        return _version(result.version)

    @router.post(
        "/{tool_id}/versions/{version_id}/withdraw",
        response_model=HttpToolVersionResponse,
    )
    async def withdraw_version(  # pyright: ignore[reportUnusedFunction]
        tool_id: UUID,
        version_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[
            HttpToolCatalog, Depends(catalog_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> HttpToolVersionResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            withdrawn = await catalog.withdraw_version(
                _actor(user),
                workspace_id,
                tool_id,
                version_id,
                request.state.request_id,
            )
        except ForbiddenHttpToolAction as error:
            raise forbidden() from error
        except UnknownHttpTool as error:
            raise _tool_not_found() from error
        except UnknownHttpToolVersion as error:
            raise AppError(
                code="http_tool_version_not_found",
                title="Version not found",
                status=404,
                detail="No version by that identifier belongs to this tool.",
            ) from error
        return _version(withdrawn)

    return router


def _actor(user: AuthenticatedUser) -> Actor:
    return Actor(user.id, user.is_platform_admin)


def _tool(tool: HttpTool) -> HttpToolResponse:
    return HttpToolResponse(
        id=tool.id,
        workspace_id=tool.workspace_id,
        name=tool.name,
        base_url=tool.base_url,
        credential_ref=tool.credential_ref,
        current_version_id=tool.current_version_id,
        created_at=tool.created_at,
        updated_at=tool.updated_at,
    )


def _version(version: HttpToolVersion) -> HttpToolVersionResponse:
    return HttpToolVersionResponse(
        id=version.id,
        http_tool_id=version.http_tool_id,
        version_number=version.version_number,
        content_hash=version.content_hash,
        title=version.title,
        document_version=version.document_version,
        operations=[
            OperationResponse(
                operation_id=operation.operation_id,
                method=operation.method,
                path=operation.path,
                summary=operation.summary,
                read_only=operation.read_only,
            )
            for operation in version.operations
        ],
        status=version.status.value,
        bindable=version.bindable,
        created_at=version.created_at,
    )


def _tool_not_found() -> AppError:
    return AppError(
        code="http_tool_not_found",
        title="Tool not found",
        status=404,
        detail="No tool by that identifier is available from this workspace.",
    )


def _invalid_document(error: InvalidOpenApiDocument) -> AppError:
    return AppError(
        code="invalid_openapi_document",
        title="Invalid OpenAPI document",
        status=422,
        detail=error.reason,
    )


def _outside_scope(error: HostOutsideWorkspaceScope) -> AppError:
    """Refused with the host named, and with what to do about it.

    A registration that fails saying only "not allowed" sends the author to
    read the code; naming the host and the page that grants it sends them to
    the person who can approve it.
    """
    return AppError(
        code="host_outside_workspace_scope",
        title="Host not approved",
        status=422,
        detail=(
            f"{error.host} is not in this workspace's outbound scope. "
            "A workspace administrator approves it under Outbound scope first."
        ),
    )
