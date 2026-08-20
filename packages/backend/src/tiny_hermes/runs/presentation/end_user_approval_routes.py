"""§5's missing door: the end user who owns a `user_confirmation` answers it.

`ApprovalType.USER_CONFIRMATION` has had a producer since 0032
(`SqlApprovalGate._subject` returns `run.end_user_id`) and design v2.5 §4.6's
matrix is explicit that this approval type is 仅发起人本人 — only the Run's
own end user, ever. Until this router, nobody with that identity could reach
it: `approval_routes.py` is `_CONSOLE_ONLY`, and `reject_end_user_caller`
refuses any request carrying the end-user cookie before `may_decide` is ever
asked. A `caller_type=end_user` Run that tripped `WritePolicy.GOVERNANCE`
opened a confirmation nobody could answer and sat `waiting_approval` until the
scheduler expired it — a producer with no consumer being worse than neither,
because it looks like the write is pending a decision rather than stuck.

Reuses `ApprovalService.decide` and `runs/domain/approval.py::may_decide`
exactly as `approval_routes.py` does — not a second copy of "who may decide
what". `may_decide` already refuses a `governance_approval` to anyone who
isn't a workspace or platform administrator, so an end user is turned away
from one the same way `ForbiddenApprovalAction` turns an administrator away
from somebody else's `user_confirmation`: no exception is carved out here,
because none needs to be.

Never `_CONSOLE_ONLY`, for the reason `end_user_run_router` gives its own
module docstring: every route here authenticates with
`resolve_end_user_caller`, not a workspace Role, so there is no console
session for that guard to reject in the first place.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Request

from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.identity.application.end_user_service import EndUserIdentityService
from tiny_hermes.identity.presentation.end_user_dependencies import (
    END_USER_SESSION_COOKIE,
    resolve_end_user_caller,
)
from tiny_hermes.runs.application.approvals import (
    ApprovalAlreadyDecided,
    ApprovalExpired,
    ApprovalService,
    ForbiddenApprovalAction,
    ReasonRequired,
    UnknownApproval,
)
from tiny_hermes.runs.domain.approval import ApprovalDecision
from tiny_hermes.runs.presentation.approval_routes import ApprovalResponse, DecideApprovalRequest
from tiny_hermes.shared.errors import AppError
from tiny_hermes.tenancy.domain.models import Actor

EndUserSessionCookie = Annotated[str | None, Cookie(alias=END_USER_SESSION_COOKIE)]


def end_user_approval_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/end-user/approvals", tags=["end-user-approvals"])
    identity_dependency = resources.end_user_identity_service
    service_dependency = resources.approval_service

    @router.post("/{approval_id}/decision", response_model=ApprovalResponse)
    async def decide(  # pyright: ignore[reportUnusedFunction]
        approval_id: UUID,
        payload: DecideApprovalRequest,
        request: Request,
        identity: Annotated[
            EndUserIdentityService, Depends(identity_dependency, scope="function")
        ],
        service: Annotated[
            ApprovalService, Depends(service_dependency, scope="function")
        ],
        end_user_session: EndUserSessionCookie = None,
    ) -> ApprovalResponse:
        caller = await resolve_end_user_caller(identity, end_user_session)
        try:
            decided = await service.decide(
                # `is_platform_admin=False`, always: an end user is never a
                # platform member, and `ApprovalService._decider` reads
                # workspace-admin status from `Membership`, which an end
                # user's id can never appear in either. `may_decide` is what
                # actually grants this identity a `user_confirmation` — by
                # matching `requested_by`, not by anything asserted here.
                Actor(caller.end_user_id, is_platform_admin=False),
                caller.workspace_id,
                approval_id,
                ApprovalDecision(payload.decision),
                payload.reason,
                request.state.request_id,
            )
        except ForbiddenApprovalAction as error:
            # No detail about which rule stopped them, same reasoning as the
            # console route: telling a wrong end user "only the Run's own
            # end user may confirm this" would tell them who is.
            raise _forbidden() from error
        except UnknownApproval as error:
            raise AppError(
                code="approval_not_found",
                title="Approval not found",
                status=404,
                detail="No approval by that identifier is waiting for you.",
            ) from error
        except ApprovalAlreadyDecided as error:
            raise AppError(
                code="approval_already_decided",
                title="Already decided",
                status=409,
                detail=f"This approval was already {error.status.value}.",
            ) from error
        except ApprovalExpired as error:
            raise AppError(
                code="approval_expired",
                title="Approval expired",
                status=409,
                detail=(
                    "This approval ran out before it was answered. The Run "
                    "has been paused; ask the Agent again to resume it."
                ),
            ) from error
        except ReasonRequired as error:
            raise AppError(
                code="approval_reason_required",
                title="A reason is required",
                status=422,
                detail="Say why, so the Agent knows what to do differently.",
            ) from error
        return ApprovalResponse.from_domain(decided)

    return router


def _forbidden() -> AppError:
    return AppError(
        code="forbidden",
        title="Forbidden",
        status=403,
        detail="This confirmation is not yours to answer.",
    )
