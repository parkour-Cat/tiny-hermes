"""§4.6's "本人" row, as four routes and one procedure.

Everything here is about one subject's own data. The subject is a path
parameter rather than something read from the session, because a workspace
administrator acting on somebody's behalf is a case §4.6 explicitly allows —
audited. The service decides which of the two is happening; these routes only
carry the request.

Erasure has its own route and its own verb, and it does not pretend to be a
tidy-up. It removes the subject's memories, sessions, messages and files, and
the reply says how many of each went — which is the same thing the audit record
says, and the only way afterwards to tell an erasure from one that never ran.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, Request
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
from tiny_hermes.memory.application.service import InvalidMemoryBody
from tiny_hermes.memory.application.subject_service import (
    ForbiddenSubjectAction,
    SubjectService,
    UnknownSubject,
    UnknownSubjectMemory,
)
from tiny_hermes.memory.domain.policy import MAX_BODY_LENGTH
from tiny_hermes.memory.presentation.routes import MemoryResponse
from tiny_hermes.shared.errors import AppError
from tiny_hermes.tenancy.domain.models import Actor

WorkspaceHeader = Annotated[str | None, Header(alias="X-Workspace-Id")]
CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token")]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


class CorrectMemoryRequest(BaseModel):
    body: str = Field(min_length=1, max_length=MAX_BODY_LENGTH)


class ResolvedSubjectResponse(BaseModel):
    subject_id: UUID
    channel: str
    external_user_id: str
    #: §344 keeps the row after an erasure. "Already erased, on this date"
    #: and "no such person" are different answers to a second request from
    #: the same person, and only one of them is true.
    erased_at: datetime | None
    first_seen_at: datetime


class SubjectExportResponse(BaseModel):
    subject_type: str
    subject_id: UUID
    workspace_id: UUID
    memories: list[MemoryResponse]
    sessions: list[UUID]


class ErasureResponse(BaseModel):
    """Counts, never content. See the module docstring."""

    memories: int
    sessions: int
    messages: int
    artifacts: int


def subject_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/subjects", tags=["subjects"])
    auth_dependency = resources.auth_service
    service_dependency = resources.subject_service

    @router.get("/lookup", response_model=ResolvedSubjectResponse)
    async def lookup(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        service: Annotated[
            SubjectService, Depends(service_dependency, scope="function")
        ],
        channel: str,
        external_user_id: str,
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> ResolvedSubjectResponse:
        """The subject a data-rights request names, by their external id.

        Every route below takes `subject_id` — a uuid this platform minted —
        and a request arrives naming a person the way the enterprise's own
        directory names them. Without this the four of them were reachable
        only by reading the database by hand.
        """
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            found = await service.lookup(
                _actor(user),
                workspace_id,
                channel,
                external_user_id,
                request.state.request_id,
            )
        except ForbiddenSubjectAction as error:
            raise forbidden() from error
        except UnknownSubject as error:
            raise AppError(
                code="subject_not_found",
                title="Subject not found",
                status=404,
                # The name is not repeated. This endpoint answers whether a
                # named person is known here, and an error page carrying the
                # name puts it into logs and a browser's history for somebody
                # who turned out not to exist.
                detail="No end user of this workspace goes by that name on that channel.",
            ) from error
        return ResolvedSubjectResponse(
            subject_id=found.subject_id,
            channel=found.channel,
            external_user_id=found.external_user_id,
            erased_at=found.erased_at,
            first_seen_at=found.first_seen_at,
        )

    @router.get("/{subject_id}/export", response_model=SubjectExportResponse)
    async def export(  # pyright: ignore[reportUnusedFunction]
        subject_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        service: Annotated[
            SubjectService, Depends(service_dependency, scope="function")
        ],
        agent_id: UUID | None = None,
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> SubjectExportResponse:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            subject = await service.subject_in(workspace_id, subject_id)
            exported = await service.export(
                _actor(user),
                workspace_id,
                subject,
                agent_id,
                request.state.request_id,
            )
        except ForbiddenSubjectAction as error:
            raise forbidden() from error
        except UnknownSubject as error:
            raise _no_such_subject() from error
        return SubjectExportResponse(
            subject_type=exported.subject.caller_type.value,
            subject_id=exported.subject.caller_id,
            workspace_id=exported.workspace_id,
            memories=[MemoryResponse.from_domain(item) for item in exported.memories],
            sessions=list(exported.sessions),
        )

    @router.post("/memories/{memory_id}/correct", response_model=MemoryResponse)
    async def correct(  # pyright: ignore[reportUnusedFunction]
        memory_id: UUID,
        payload: CorrectMemoryRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        service: Annotated[
            SubjectService, Depends(service_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> MemoryResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            corrected = await service.correct(
                _actor(user),
                workspace_id,
                memory_id,
                payload.body,
                request.state.request_id,
            )
        except ForbiddenSubjectAction as error:
            raise forbidden() from error
        except UnknownSubjectMemory as error:
            raise _not_found() from error
        except InvalidMemoryBody as error:
            raise AppError(
                code="invalid_memory_body",
                title="Invalid memory",
                status=422,
                detail=str(error),
            ) from error
        return MemoryResponse.from_domain(corrected)

    @router.post("/memories/{memory_id}/forget", response_model=MemoryResponse)
    async def forget(  # pyright: ignore[reportUnusedFunction]
        memory_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        service: Annotated[
            SubjectService, Depends(service_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> MemoryResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            removed = await service.forget(
                _actor(user), workspace_id, memory_id, request.state.request_id
            )
        except ForbiddenSubjectAction as error:
            raise forbidden() from error
        except UnknownSubjectMemory as error:
            raise _not_found() from error
        return MemoryResponse.from_domain(removed)

    @router.post("/{subject_id}/erase", response_model=ErasureResponse)
    async def erase(  # pyright: ignore[reportUnusedFunction]
        subject_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        service: Annotated[
            SubjectService, Depends(service_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> ErasureResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            report = await service.erase(
                _actor(user),
                workspace_id,
                await service.subject_in(workspace_id, subject_id),
                request.state.request_id,
            )
        except ForbiddenSubjectAction as error:
            raise forbidden() from error
        except UnknownSubject as error:
            raise _no_such_subject() from error
        return ErasureResponse(
            memories=report.memories,
            sessions=report.sessions,
            messages=report.messages,
            artifacts=report.artifacts,
        )

    return router


def _no_such_subject() -> AppError:
    """One refusal for "no such id" and for "not in this workspace".

    Telling them apart would let a steward of one tenant confirm that a
    given id is a subject of another.
    """
    return AppError(
        code="subject_not_found",
        title="Subject not found",
        status=404,
        detail="No subject of this workspace has that identifier.",
    )


def _actor(user: AuthenticatedUser) -> Actor:
    return Actor(user.id, user.is_platform_admin)


def _not_found() -> AppError:
    return AppError(
        code="memory_not_found",
        title="Memory not found",
        status=404,
        detail="No memory by that identifier belongs to you in this workspace.",
    )
