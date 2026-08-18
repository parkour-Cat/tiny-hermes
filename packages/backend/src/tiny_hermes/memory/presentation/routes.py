"""The memory review's HTTP face: what is waiting, decide one, edit shared.

Three routes, and deliberately not the M2C approval routes with a different
noun. A pending memory holds up no Run, so there is no state machine here and
no expiry — the queue is the `pending` rows, and deciding one flips a status
and writes an audit line. §4.6 gates who may: a workspace or platform
administrator, and the service refuses everyone else before a row is touched.

The self-service routes a subject uses on their own memory — view, correct,
delete, export — are the plan's §6 and land with the erasure flow.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, Query, Request, status
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
from tiny_hermes.memory.application.service import (
    ForbiddenMemoryAction,
    InvalidMemoryBody,
    MemoryAlreadyDecided,
    MemoryRecord,
    MemoryService,
    UnknownAgent,
    UnknownMemory,
)
from tiny_hermes.memory.domain.policy import MAX_BODY_LENGTH
from tiny_hermes.memory.domain.search import (
    MAX_QUERY_CHARS,
    SearchHit,
    SearchRefused,
    request_for,
)
from tiny_hermes.memory.infrastructure.sql_search import SqlSessionSearch
from tiny_hermes.shared.errors import AppError
from tiny_hermes.tenancy.domain.models import Actor

WorkspaceHeader = Annotated[str | None, Header(alias="X-Workspace-Id")]
CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token")]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


class SearchHitResponse(BaseModel):
    session_id: str
    run_id: str | None
    sequence: int
    role: str
    snippet: str
    #: True when the message was longer than one snippet. Shown rather than
    #: hidden: a reader who does not know they are holding part of a message
    #: reads it as the whole of one.
    shortened: bool

    @classmethod
    def from_domain(cls, hit: SearchHit) -> "SearchHitResponse":
        return cls(
            session_id=hit.session_id,
            run_id=hit.run_id,
            sequence=hit.sequence,
            role=hit.role,
            snippet=hit.snippet,
            shortened=hit.shortened,
        )


class CreateSharedRequest(BaseModel):
    agent_id: UUID
    body: str = Field(min_length=1, max_length=MAX_BODY_LENGTH)


class MemoryResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    agent_id: UUID
    kind: str
    status: str
    body: str
    origin: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, record: MemoryRecord) -> "MemoryResponse":
        return cls(
            id=record.id,
            workspace_id=record.workspace_id,
            agent_id=record.agent_id,
            kind=record.kind.value,
            status=record.status.value,
            body=record.body,
            origin=record.origin,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


def memory_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/memories", tags=["memories"])
    auth_dependency = resources.auth_service
    service_dependency = resources.memory_service
    search_dependency = resources.session_search

    @router.get("/pending", response_model=list[MemoryResponse])
    async def list_pending(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        service: Annotated[
            MemoryService, Depends(service_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> list[MemoryResponse]:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            listed = await service.list_pending(
                _actor(user), workspace_id, request.state.request_id
            )
        except ForbiddenMemoryAction as error:
            raise forbidden() from error
        return [MemoryResponse.from_domain(item) for item in listed]

    @router.post("/{memory_id}/approve", response_model=MemoryResponse)
    async def approve(  # pyright: ignore[reportUnusedFunction]
        memory_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        service: Annotated[
            MemoryService, Depends(service_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> MemoryResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            decided = await service.approve(
                _actor(user), workspace_id, memory_id, request.state.request_id
            )
        except ForbiddenMemoryAction as error:
            raise forbidden() from error
        except UnknownMemory as error:
            raise _not_found() from error
        except MemoryAlreadyDecided as error:
            raise _already_decided(error) from error
        return MemoryResponse.from_domain(decided)

    @router.post("/{memory_id}/reject", response_model=MemoryResponse)
    async def reject(  # pyright: ignore[reportUnusedFunction]
        memory_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        service: Annotated[
            MemoryService, Depends(service_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> MemoryResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            decided = await service.reject(
                _actor(user), workspace_id, memory_id, request.state.request_id
            )
        except ForbiddenMemoryAction as error:
            raise forbidden() from error
        except UnknownMemory as error:
            raise _not_found() from error
        except MemoryAlreadyDecided as error:
            raise _already_decided(error) from error
        return MemoryResponse.from_domain(decided)

    @router.get("/search", response_model=list[SearchHitResponse])
    async def search_sessions(  # pyright: ignore[reportUnusedFunction]
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        searches: Annotated[
            SqlSessionSearch, Depends(search_dependency, scope="function")
        ],
        service: Annotated[
            MemoryService, Depends(service_dependency, scope="function")
        ],
        request: Request,
        q: str = Query(min_length=1, max_length=MAX_QUERY_CHARS),
        limit: int | None = None,
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> list[SearchHitResponse]:
        """Search this workspace's sessions, for somebody who may read them.

        §4.6 decides that, and it is the same "steward" test reviewing a memory
        needs: a workspace or platform administrator. A subject's own search of
        their own history goes through `session.search` inside a Run, and the
        self-service route lands with the plan's §6.
        """
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            # Borrowed rather than duplicated: one definition of who may look
            # at somebody else's conversations, and it lives with the rest of
            # §4.6's answers.
            await service.list_pending(
                _actor(user), workspace_id, request.state.request_id
            )
        except ForbiddenMemoryAction as error:
            raise forbidden() from error
        try:
            asked = request_for(q, limit)
        except SearchRefused as error:
            raise AppError(
                code="invalid_search",
                title="Invalid search",
                status=422,
                detail=str(error),
            ) from error
        hits = await searches.for_workspace(workspace_id, asked)
        return [SearchHitResponse.from_domain(hit) for hit in hits]

    @router.post(
        "/shared",
        response_model=MemoryResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_shared(  # pyright: ignore[reportUnusedFunction]
        payload: CreateSharedRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        service: Annotated[
            MemoryService, Depends(service_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> MemoryResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            created = await service.create_shared(
                _actor(user),
                workspace_id,
                agent_id=payload.agent_id,
                body=payload.body,
                request_id=request.state.request_id,
            )
        except ForbiddenMemoryAction as error:
            raise forbidden() from error
        except UnknownAgent as error:
            raise AppError(
                code="agent_not_found",
                title="Agent not found",
                status=404,
                detail="No Agent by that identifier is in this workspace.",
            ) from error
        except InvalidMemoryBody as error:
            raise AppError(
                code="invalid_memory_body",
                title="Invalid memory",
                status=422,
                detail=str(error),
            ) from error
        return MemoryResponse.from_domain(created)

    return router


def _actor(user: AuthenticatedUser) -> Actor:
    return Actor(user.id, user.is_platform_admin)


def _not_found() -> AppError:
    return AppError(
        code="memory_not_found",
        title="Memory not found",
        status=404,
        detail="No memory by that identifier is waiting in this workspace.",
    )


def _already_decided(error: MemoryAlreadyDecided) -> AppError:
    return AppError(
        code="memory_already_decided",
        title="Already decided",
        status=409,
        detail=f"This candidate was already {error.status.value}.",
    )
