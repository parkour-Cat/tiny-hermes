"""§4.6's "本人" row, for the subject `subject_routes.py` cannot reach.

That router authenticates with `resolve_workspace_caller` and takes a
`subject_id` path parameter, because it serves two audiences design §4.6
allows: a platform member acting on their own data, and a workspace
administrator acting on somebody else's, audited. It is also `_CONSOLE_ONLY`
in `api/app.py` — an end user gets 403 from it with no exceptions, the same
guard `runs/presentation/end_user_routes.py`'s own docstring explains.

An end user still has §4.6's row — export, correct, forget, erase are their
data too — so it needs a second door that is never console-gated. `Subject
Service` itself does not change: every route here builds the exact
`CallerIdentity`/`Actor` pair `_require_self_or_steward` already knows how to
approve for "acting on my own data" (`actor.id == subject.caller_id` is
`True` by construction, so the steward branch is never reached), and calls
the same four methods `subject_routes.py` calls. What changes is only how the
caller is authenticated and that there is no `subject_id` to read from a
path — an end user's own id is the only one this door will ever act on, so
there is nothing here for a `subject_id` parameter to mean.

`correct`, `forget`, and `erase` authenticate with `resolve_end_user_caller_
for_write` rather than `resolve_end_user_caller` — design §7's origin check,
because these three change state and this cookie is `SameSite=None` with no
`X-CSRF-Token` to fall back on. `export` stays on the plain read path: a GET
that only returns this end user's own data to them is not the request shape
that check exists for.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Request
from pydantic import BaseModel, Field

from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.identity.application.end_user_service import EndUserIdentityService
from tiny_hermes.identity.presentation.end_user_dependencies import (
    END_USER_SESSION_COOKIE,
    resolve_end_user_caller,
    resolve_end_user_caller_for_write,
)
from tiny_hermes.memory.application.service import InvalidMemoryBody
from tiny_hermes.memory.application.subject_service import (
    ForbiddenSubjectAction,
    SubjectService,
    UnknownSubjectMemory,
)
from tiny_hermes.memory.domain.policy import MAX_BODY_LENGTH
from tiny_hermes.memory.presentation.routes import MemoryResponse
from tiny_hermes.runs.domain.models import CallerIdentity, CallerType
from tiny_hermes.shared.errors import AppError
from tiny_hermes.tenancy.domain.models import Actor

EndUserSessionCookie = Annotated[str | None, Cookie(alias=END_USER_SESSION_COOKIE)]


class CorrectMemoryRequest(BaseModel):
    body: str = Field(min_length=1, max_length=MAX_BODY_LENGTH)


class SubjectExportResponse(BaseModel):
    subject_type: str
    subject_id: UUID
    workspace_id: UUID
    memories: list[MemoryResponse]
    sessions: list[UUID]


class ErasureResponse(BaseModel):
    memories: int
    sessions: int
    messages: int
    artifacts: int


def end_user_subject_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/end-user/subjects", tags=["end-user-subjects"])
    identity_dependency = resources.end_user_identity_service
    service_dependency = resources.subject_service

    @router.get("/me/export", response_model=SubjectExportResponse)
    async def export(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        identity: Annotated[
            EndUserIdentityService, Depends(identity_dependency, scope="function")
        ],
        service: Annotated[SubjectService, Depends(service_dependency, scope="function")],
        agent_id: UUID | None = None,
        end_user_session: EndUserSessionCookie = None,
    ) -> SubjectExportResponse:
        caller = await resolve_end_user_caller(identity, end_user_session)
        actor, subject = _self(caller.end_user_id)
        try:
            exported = await service.export(
                actor, caller.workspace_id, subject, agent_id, request.state.request_id
            )
        except ForbiddenSubjectAction as error:  # pragma: no cover - self is always allowed
            raise _forbidden() from error
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
        identity: Annotated[
            EndUserIdentityService, Depends(identity_dependency, scope="function")
        ],
        service: Annotated[SubjectService, Depends(service_dependency, scope="function")],
        end_user_session: EndUserSessionCookie = None,
    ) -> MemoryResponse:
        caller = await resolve_end_user_caller_for_write(
            identity, end_user_session, request.headers
        )
        actor, _subject = _self(caller.end_user_id)
        try:
            corrected = await service.correct(
                actor, caller.workspace_id, memory_id, payload.body, request.state.request_id
            )
        except ForbiddenSubjectAction as error:
            raise _forbidden() from error
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
        identity: Annotated[
            EndUserIdentityService, Depends(identity_dependency, scope="function")
        ],
        service: Annotated[SubjectService, Depends(service_dependency, scope="function")],
        end_user_session: EndUserSessionCookie = None,
    ) -> MemoryResponse:
        caller = await resolve_end_user_caller_for_write(
            identity, end_user_session, request.headers
        )
        actor, _subject = _self(caller.end_user_id)
        try:
            removed = await service.forget(
                actor, caller.workspace_id, memory_id, request.state.request_id
            )
        except ForbiddenSubjectAction as error:
            raise _forbidden() from error
        except UnknownSubjectMemory as error:
            raise _not_found() from error
        return MemoryResponse.from_domain(removed)

    @router.post("/me/erase", response_model=ErasureResponse)
    async def erase(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        identity: Annotated[
            EndUserIdentityService, Depends(identity_dependency, scope="function")
        ],
        service: Annotated[SubjectService, Depends(service_dependency, scope="function")],
        end_user_session: EndUserSessionCookie = None,
    ) -> ErasureResponse:
        caller = await resolve_end_user_caller_for_write(
            identity, end_user_session, request.headers
        )
        actor, subject = _self(caller.end_user_id)
        try:
            report = await service.erase(
                actor, caller.workspace_id, subject, request.state.request_id
            )
        except ForbiddenSubjectAction as error:  # pragma: no cover - self is always allowed
            raise _forbidden() from error
        return ErasureResponse(
            memories=report.memories,
            sessions=report.sessions,
            messages=report.messages,
            artifacts=report.artifacts,
        )

    return router


def _self(end_user_id: UUID) -> tuple[Actor, CallerIdentity]:
    """The one pair every route above needs: an `Actor` whose id is this end
    user's own, and the matching `CallerIdentity` subject. `SubjectService.
    _require_self_or_steward` approves `actor.id == subject.caller_id` before
    it ever asks about roles, so this alone is what makes every call here
    "acting on my own data" rather than "acting on somebody else's" — there is
    no steward path through this router because there is no path to give it
    somebody else's id.
    """
    actor = Actor(end_user_id, is_platform_admin=False)
    subject = CallerIdentity(caller_type=CallerType.END_USER, caller_id=end_user_id)
    return actor, subject


def _forbidden() -> AppError:
    return AppError(
        code="forbidden",
        title="Forbidden",
        status=403,
        detail="You cannot perform that action.",
    )


def _not_found() -> AppError:
    return AppError(
        code="memory_not_found",
        title="Memory not found",
        status=404,
        detail="No memory by that identifier belongs to you in this workspace.",
    )
