"""The approvals' HTTP face: see what is waiting, and decide one.

Two routes, because §16.3 needs exactly two things from a person: knowing that
something is waiting, and answering it. The queue with filters, assignment and
history is M3's; this is the part without which a write cannot happen at all.

`document` goes out as it was hashed. A reviewer deciding from a summary the
platform rewrote would be approving something nobody can prove matches what
runs, and the whole point of §16.3's normalized hash is that those two are one
thing.
"""

from datetime import datetime
from typing import Annotated, Literal
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
from tiny_hermes.runs.application.approvals import (
    MAX_REASON_LENGTH,
    ApprovalAlreadyDecided,
    ApprovalExpired,
    ApprovalService,
    ForbiddenApprovalAction,
    ReasonRequired,
    UnknownApproval,
)
from tiny_hermes.runs.domain.approval import Approval, ApprovalDecision
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


def approval_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])
    auth_dependency = resources.auth_service
    service_dependency = resources.approval_service

    @router.get("", response_model=list[ApprovalResponse])
    async def list_pending(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        service: Annotated[
            ApprovalService, Depends(service_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> list[ApprovalResponse]:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            listed = await service.list_pending(
                _actor(user), workspace_id, request.state.request_id
            )
        except ForbiddenApprovalAction as error:
            raise forbidden() from error
        return [ApprovalResponse.from_domain(item) for item in listed]

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
