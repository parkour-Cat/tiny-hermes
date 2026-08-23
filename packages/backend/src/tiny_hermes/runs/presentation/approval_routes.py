"""The approvals' HTTP face: read the queue, and decide one.

§16.3 needs two things from a person — knowing that something is waiting and
answering it — and §26 adds the third: reading back what was decided. All
three go through these two routes.

The queue is one route with a `status` filter rather than a working list and
a separate history endpoint. Two doors onto the same rows drift: the day a
filter or a redaction lands on one of them, the other quietly answers a
different question. It defaults to `pending`, so a caller who says nothing
still gets the working queue and not the archive.

There is no assignment. The product design names no assignee anywhere, and
§4.6 already fixes who may decide a `governance_approval`; a queue that let
one administrator claim a row would either be advisory (a label two people
can ignore) or a second permission system beside the matrix. Neither is
something to invent here.

`document` goes out as it was hashed. A reviewer deciding from a summary the
platform rewrote would be approving something nobody can prove matches what
runs, and the whole point of §16.3's normalized hash is that those two are one
thing.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, Query, Request
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
from tiny_hermes.runs.application.approvals import (
    MAX_REASON_LENGTH,
    ApprovalAlreadyDecided,
    ApprovalExpired,
    ApprovalService,
    ForbiddenApprovalAction,
    ReasonRequired,
    UnknownApproval,
)
from tiny_hermes.runs.domain.approval import (
    Approval,
    ApprovalDecision,
    ApprovalStatus,
    ApprovalType,
)
from tiny_hermes.runs.domain.approval_query import (
    InvalidApprovalFilter,
    QueueOrder,
    filter_for,
)
from tiny_hermes.shared.errors import AppError
from tiny_hermes.tenancy.domain.models import Actor

WorkspaceHeader = Annotated[str | None, Header(alias="X-Workspace-Id")]
CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token")]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


class DecideApprovalRequest(BaseModel):
    decision: Literal["approve", "reject"]
    #: Required on a rejection. The person whose Run stopped is not the person
    #: who stopped it, and "no" without a reason gives them nothing to change.
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)


class ApprovalResponse(BaseModel):
    id: UUID
    run_id: UUID
    approval_type: str
    status: str
    tool: str
    #: The normalized call, exactly as hashed. See the module docstring.
    document: dict[str, object]
    required_permission: str | None
    requested_by: UUID
    expires_at: datetime
    created_at: datetime | None = None
    decided_by: UUID | None = None
    decided_at: datetime | None = None
    decision_reason: str | None = None

    @classmethod
    def from_domain(cls, approval: Approval) -> "ApprovalResponse":
        return cls(
            id=approval.id,
            run_id=approval.run_id,
            approval_type=approval.approval_type.value,
            status=approval.status.value,
            tool=approval.tool,
            document=approval.document,
            required_permission=approval.required_permission,
            requested_by=approval.requested_by,
            expires_at=approval.expires_at,
            decided_by=approval.decided_by,
            decided_at=approval.decided_at,
            decision_reason=approval.decision_reason,
        )


class ApprovalsPageResponse(BaseModel):
    items: list[ApprovalResponse]
    #: Whether a later offset holds more. A queue that ended at the page
    #: boundary with no sign of it reads as "nothing else is waiting".
    has_more: bool


#: What a caller writes to mean "every status". `status=` with no value is
#: indistinguishable from the parameter being absent over a query string,
#: and "absent" already means the working queue.
ANY_STATUS = "any"


def _statuses(asked: list[str] | None) -> tuple[ApprovalStatus, ...] | None:
    if asked is None:
        return None
    if ANY_STATUS in asked:
        return ()
    try:
        return tuple(ApprovalStatus(value) for value in asked)
    except ValueError as error:
        # Refused, not dropped. A misspelled status that is ignored hands the
        # caller the default queue under the heading they asked for.
        raise AppError(
            code="invalid_approval_filter",
            title="Invalid filter",
            status=400,
            detail=f"Unknown approval status. Expected one of: "
            f"{', '.join(item.value for item in ApprovalStatus)}, or '{ANY_STATUS}'.",
        ) from error


def _approval_type(asked: str | None) -> ApprovalType | None:
    if asked is None:
        return None
    try:
        return ApprovalType(asked)
    except ValueError as error:
        raise AppError(
            code="invalid_approval_filter",
            title="Invalid filter",
            status=400,
            detail=f"Unknown approval type. Expected one of: "
            f"{', '.join(item.value for item in ApprovalType)}.",
        ) from error


def _order(asked: str | None) -> QueueOrder | None:
    if asked is None:
        return None
    try:
        return QueueOrder(asked)
    except ValueError as error:
        raise AppError(
            code="invalid_approval_filter",
            title="Invalid filter",
            status=400,
            detail=f"Unknown order. Expected one of: "
            f"{', '.join(item.value for item in QueueOrder)}.",
        ) from error


def approval_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])
    auth_dependency = resources.auth_service
    service_dependency = resources.approval_service

    @router.get("", response_model=ApprovalsPageResponse)
    async def list_approvals(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        service: Annotated[
            ApprovalService, Depends(service_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        # Repeatable: `?status=approved&status=rejected` is "answered". An
        # explicit empty list is not expressible over a query string, so
        # `status=any` carries §26's "everything" — see `_statuses`.
        status: Annotated[list[str] | None, Query()] = None,
        approval_type: str | None = None,
        tool: str | None = None,
        decided_by: UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        order: str | None = None,
        # No `le=` here: `filter_for` clamps an over-large page rather than
        # refusing it, the same rule `audit/presentation/routes.py` follows,
        # and an HTTP ceiling would 422 exactly the requests it means to cap.
        limit: Annotated[int | None, Query(ge=1)] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> ApprovalsPageResponse:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            criteria = filter_for(
                statuses=_statuses(status),
                approval_type=_approval_type(approval_type),
                tool=tool,
                decided_by=decided_by,
                since=since,
                until=until,
                order=_order(order),
                limit=limit,
                offset=offset,
            )
        except InvalidApprovalFilter as error:
            raise AppError(
                code="invalid_approval_filter",
                title="Invalid filter",
                status=400,
                detail=str(error),
            ) from error
        try:
            page = await service.list_approvals(
                _actor(user), workspace_id, criteria, request.state.request_id
            )
        except ForbiddenApprovalAction as error:
            raise forbidden() from error
        return ApprovalsPageResponse(
            items=[ApprovalResponse.from_domain(item) for item in page.items],
            has_more=page.has_more,
        )

    @router.post("/{approval_id}/decision", response_model=ApprovalResponse)
    async def decide(  # pyright: ignore[reportUnusedFunction]
        approval_id: UUID,
        payload: DecideApprovalRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        service: Annotated[
            ApprovalService, Depends(service_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> ApprovalResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            decided = await service.decide(
                _actor(user),
                workspace_id,
                approval_id,
                ApprovalDecision(payload.decision),
                payload.reason,
                request.state.request_id,
            )
        except ForbiddenApprovalAction as error:
            # 403 with no detail about which rule stopped them. Saying "only the
            # Run's end user may confirm this" to somebody who is not them would
            # tell them who is.
            raise forbidden() from error
        except UnknownApproval as error:
            raise AppError(
                code="approval_not_found",
                title="Approval not found",
                status=404,
                detail="No approval by that identifier is waiting in this workspace.",
            ) from error
        except ApprovalAlreadyDecided as error:
            raise AppError(
                code="approval_already_decided",
                title="Already decided",
                status=409,
                detail=(
                    f"This approval was already {error.status.value}. A management "
                    "override is a new governance approval, never a rewrite of "
                    "this one."
                ),
            ) from error
        except ApprovalExpired as error:
            raise AppError(
                code="approval_expired",
                title="Approval expired",
                status=409,
                detail=(
                    "This approval ran out before it was answered. The Run has "
                    "been paused; resubmit it to ask again."
                ),
            ) from error
        except ReasonRequired as error:
            raise AppError(
                code="approval_reason_required",
                title="A reason is required",
                status=422,
                detail="Say why, so the person whose Run stopped can act on it.",
            ) from error
        return ApprovalResponse.from_domain(decided)

    return router


def _actor(user: AuthenticatedUser) -> Actor:
    return Actor(user.id, user.is_platform_admin)
