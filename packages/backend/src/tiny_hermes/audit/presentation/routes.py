"""§4: the one HTTP door onto `audit_events` this codebase has ever had.

`GET /api/v1/audit-events` — paginated, filterable, newest first. Four of
§4.6's five subjects reach it; the fifth (终端用户) never does, refused
twice over: `_CONSOLE_ONLY` (`api/app.py`) rejects the end-user cookie
before this router's own code runs at all, and `AuditService.list_events`
refuses an end-user `Actor` again on its own, the same belt-and-suspenders
`ApprovalService`'s own refusals use.

**Two response models, not one model plus an `if`** — commits `19b91e3`
and `b97ddb3`'s own precedent (`runs/presentation/end_user_routes.py`).
`AuditEventResponse.context` is `dict[str, Any]`; a viewer's own
`RedactedAuditEventResponse.context` is `dict[str, str]`, and it is built
only from an `AuditRecord` `AuditService` has already redacted — this route
picks which class to build with `AuditReadResult.visibility`, the one fact
`AuditService.list_events` hands back precisely so this layer never has to
ask `user_role` a second, independently-drifting question.
"""

import csv
import io
import json
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, Query, Request, Response
from pydantic import BaseModel

from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.audit.application.audit_service import AuditService, ForbiddenAuditRead
from tiny_hermes.audit.domain.query import (
    MAX_EXPORT_ROWS,
    InvalidAuditFilter,
    export_filter_for,
    filter_for,
)
from tiny_hermes.audit.domain.record import AuditRecord
from tiny_hermes.audit.domain.scope import AuditVisibility
from tiny_hermes.identity.application.auth_service import AuthService
from tiny_hermes.identity.domain.models import AuthenticatedUser
from tiny_hermes.identity.presentation.dependencies import (
    SESSION_COOKIE,
    authenticate_browser_user,
    forbidden,
    require_workspace_id,
)
from tiny_hermes.shared.errors import AppError
from tiny_hermes.tenancy.domain.models import Actor

WorkspaceHeader = Annotated[str | None, Header(alias="X-Workspace-Id")]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


class AuditEventResponse(BaseModel):
    """Every column but `context` is an identifier, safe for anyone who can
    read the row at all (§2's own reasoning) — this is what a workspace
    administrator, a platform administrator, and a developer's own
    in-scope rows all come back as."""

    id: UUID
    workspace_id: UUID | None
    actor_type: str
    actor_id: UUID | None
    action: str
    resource_type: str
    resource_id: UUID | None
    result: str
    request_id: str
    context: dict[str, Any]
    created_at: datetime

    @classmethod
    def from_domain(cls, record: AuditRecord) -> "AuditEventResponse":
        return cls(
            id=record.id,
            workspace_id=record.workspace_id,
            actor_type=record.actor_type,
            actor_id=record.actor_id,
            action=record.action,
            resource_type=record.resource_type,
            resource_id=record.resource_id,
            result=record.result,
            request_id=record.request_id,
            context=record.context,
            created_at=record.created_at,
        )


class RedactedAuditEventResponse(BaseModel):
    """A 查看者's own shape. `context` is typed `dict[str, str]` and built
    only from a record `AuditService` has already run through
    `redact_context` — this class does not redact anything itself, and
    cannot un-redact a dict that has already lost its keys. What it does
    guarantee is narrower than that: the route's own return-type annotation
    for the viewer branch never claims to carry the fuller shape, so a
    future change that pipes a `FULL`-visibility result through this
    branch is a type error here, not a silent leak discovered later.
    """

    id: UUID
    workspace_id: UUID | None
    actor_type: str
    actor_id: UUID | None
    action: str
    resource_type: str
    resource_id: UUID | None
    result: str
    request_id: str
    context: dict[str, str]
    created_at: datetime

    @classmethod
    def from_domain(cls, record: AuditRecord) -> "RedactedAuditEventResponse":
        return cls(
            id=record.id,
            workspace_id=record.workspace_id,
            actor_type=record.actor_type,
            actor_id=record.actor_id,
            action=record.action,
            resource_type=record.resource_type,
            resource_id=record.resource_id,
            result=record.result,
            request_id=record.request_id,
            context={str(k): str(v) for k, v in record.context.items()},
            created_at=record.created_at,
        )


class AuditEventsPage(BaseModel):
    items: list[AuditEventResponse]
    has_more: bool
    #: Which of §4.6's five ranges produced this page. It is on the wire
    #: because the two page shapes are otherwise indistinguishable as JSON —
    #: both are `{items, has_more}`, and a redacted `context` is `{}`, which
    #: is exactly what a row that never carried one looks like. Without this
    #: field a reader cannot tell "nothing was recorded" from "you may not
    #: see what was recorded", and those two lead to opposite conclusions.
    #: A console that redacted silently would be handing someone incomplete
    #: evidence with no sign that it was incomplete.
    visibility: AuditVisibility


class RedactedAuditEventsPage(BaseModel):
    items: list[RedactedAuditEventResponse]
    has_more: bool
    #: See `AuditEventsPage.visibility`. Always `REDACTED` here — carried
    #: rather than assumed so that one client-side check works against
    #: either shape.
    visibility: AuditVisibility


def audit_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/audit-events", tags=["audit"])
    auth_dependency = resources.auth_service
    service_dependency = resources.audit_service

    @router.get("/export")
    async def export_events(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        service: Annotated[AuditService, Depends(service_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        action: str | None = None,
        resource_type: str | None = None,
        actor_id: UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> Response:
        """§26's export, over the same call the page above makes.

        Deliberately `service.list_events` rather than a query of its own.
        An export is a second door onto the same rows, and the whole risk is
        that the two doors disagree: a viewer whose page hides `context` but
        whose spreadsheet contains it has been handed the thing the API
        refuses them, and nothing would look wrong — the export "worked".
        Sharing the call makes that disagreement impossible rather than
        unlikely, and the cross-workspace trace (§3) comes along with it.

        No `limit`/`offset`: an export is the whole of what this reader may
        see under these filters, and a paginated export is a file somebody
        has to reassemble. `MAX_EXPORT_ROWS` is a ceiling rather than a page
        — a caller who hits it is told, not silently truncated.
        """
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            built = export_filter_for(
                action=action,
                resource_type=resource_type,
                actor_id=actor_id,
                since=since,
                until=until,
            )
        except InvalidAuditFilter as error:
            raise AppError(
                code="invalid_audit_filter",
                title="Invalid filter",
                status=400,
                detail=str(error),
            ) from error
        try:
            result = await service.list_events(
                _actor(user), workspace_id, built, request.state.request_id
            )
        except ForbiddenAuditRead as error:
            raise forbidden() from error

        # `has_more` is the store's own limit+1 answer, so reaching it means
        # the window genuinely holds more than one export may carry. Refusing
        # is the point: a 200 with a truncated file is indistinguishable from
        # a complete one, and this is an audit trail.
        if result.has_more:
            raise AppError(
                code="audit_export_too_large",
                title="Too many rows to export",
                status=413,
                detail=(
                    f"This filter matches more than {MAX_EXPORT_ROWS} events. "
                    "Narrow the time window or the action and export again."
                ),
            )

        rows = io.StringIO()
        writer = csv.writer(rows)
        writer.writerow(
            [
                "occurred_at",
                "actor_type",
                "actor_id",
                "action",
                "resource_type",
                "resource_id",
                "result",
                "request_id",
                "context",
            ]
        )
        for item in result.items:
            # `context` is already redacted for a viewer — `list_events` did
            # it. Serialized as JSON in one cell rather than spread into
            # columns, because its keys differ per action and a spreadsheet
            # with a column per key would be mostly empty.
            writer.writerow(
                [
                    item.created_at.isoformat(),
                    item.actor_type,
                    "" if item.actor_id is None else str(item.actor_id),
                    item.action,
                    item.resource_type,
                    "" if item.resource_id is None else str(item.resource_id),
                    item.result,
                    item.request_id,
                    json.dumps(item.context, ensure_ascii=False, sort_keys=True),
                ]
            )
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        return Response(
            content=rows.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="audit-{stamp}.csv"'
                ),
                # Says which of §4.6's ranges produced the file, for the same
                # reason the JSON page carries it: a redacted export and a
                # full one are the same shape, and only this tells them apart.
                "X-Audit-Visibility": result.visibility.value,
            },
        )

    @router.get("", response_model=AuditEventsPage | RedactedAuditEventsPage)
    async def list_events(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        service: Annotated[AuditService, Depends(service_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        action: str | None = None,
        resource_type: str | None = None,
        actor_id: UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        # No `le=` here on purpose: `filter_for` clamps a limit above
        # `MAX_PAGE_SIZE` rather than refusing it (§1's own domain rule,
        # mirroring `memory/domain/search.py::request_for`) — an HTTP-level
        # ceiling would 422 exactly the requests that rule means to just
        # cap, which is a different, harsher behaviour than the one this
        # module documents and tests.
        limit: Annotated[int | None, Query(ge=1)] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> AuditEventsPage | RedactedAuditEventsPage:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            built = filter_for(
                action=action,
                resource_type=resource_type,
                actor_id=actor_id,
                since=since,
                until=until,
                limit=limit,
                offset=offset,
            )
        except InvalidAuditFilter as error:
            raise AppError(
                code="invalid_audit_filter",
                title="Invalid filter",
                status=400,
                detail=str(error),
            ) from error
        try:
            result = await service.list_events(
                _actor(user), workspace_id, built, request.state.request_id
            )
        except ForbiddenAuditRead as error:
            raise forbidden() from error
        if result.visibility is AuditVisibility.REDACTED:
            return RedactedAuditEventsPage(
                items=[RedactedAuditEventResponse.from_domain(item) for item in result.items],
                has_more=result.has_more,
                visibility=result.visibility,
            )
        return AuditEventsPage(
            items=[AuditEventResponse.from_domain(item) for item in result.items],
            has_more=result.has_more,
            visibility=result.visibility,
        )

    return router


def _actor(user: AuthenticatedUser) -> Actor:
    # Matches `approval_routes.py::_actor`: a browser session names a
    # `users.id` and whether it is a platform administrator, never a
    # Role — the Role is resolved from workspace membership one layer
    # down, inside `AuditService` itself.
    return Actor(user.id, user.is_platform_admin)
