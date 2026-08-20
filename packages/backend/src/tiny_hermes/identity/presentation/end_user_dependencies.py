"""The end-user half of authentication, kept apart from
`presentation/dependencies.py` on purpose.

`resolve_workspace_caller` there is Cookie XOR Bearer for platform members
and machines — two subjects, one function, already the most subtle piece of
judgment in that module. A third subject does not get a third branch; it
gets its own function, its own cookie name (`END_USER_SESSION_COOKIE`, never
`SESSION_COOKIE`), and its own error shapes. That separation is what makes it
impossible for a review to miss a fourth subject sneaking into the platform-
member path, because there is no shared path to sneak into.

`reject_end_user_caller` is the other half of design §8's last row: an end
user must get 403 from every console endpoint, "no exceptions" — not the 401
that `resolve_workspace_caller` would give for free just because it has never
heard of this cookie. `api/app.py` wires this into every console router's
`include_router(..., dependencies=[...])`, which is what makes "no
exceptions" true for routers this task never has to open and edit one by one.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Cookie

from tiny_hermes.identity.application.end_user_service import EndUserIdentityService
from tiny_hermes.shared.errors import AppError

#: Deliberately not `tiny_hermes_session` (`presentation/dependencies.py`).
#: Design §3's own words: the two identity systems must never share a
#: credential, and a shared cookie name is a shared credential in every way
#: that counts — one name a bug could read from either side.
END_USER_SESSION_COOKIE = "tiny_hermes_end_user_session"

EndUserSessionCookie = Annotated[str | None, Cookie(alias=END_USER_SESSION_COOKIE)]


@dataclass(frozen=True)
class EndUserCaller:
    end_user_id: UUID
    workspace_id: UUID


def end_user_unauthenticated() -> AppError:
    return AppError(
        code="unauthenticated",
        title="Authentication required",
        status=401,
        detail="A valid end-user session is required.",
    )


def console_forbidden() -> AppError:
    return AppError(
        code="forbidden",
        title="Forbidden",
        status=403,
        detail="End users do not have access to console endpoints.",
    )


async def resolve_end_user_caller(
    service: EndUserIdentityService, session_token: str | None
) -> EndUserCaller:
    if not session_token:
        raise end_user_unauthenticated()
    session = await service.authenticate(session_token, datetime.now(UTC))
    if session is None:
        raise end_user_unauthenticated()
    return EndUserCaller(session.end_user_id, session.workspace_id)


def reject_end_user_caller(end_user_session: EndUserSessionCookie = None) -> None:
    """§8's last row, made into something every console router can depend on
    without knowing this feature exists. Presence alone is enough to refuse:
    checking validity would mean a database round trip on every console
    request just to decide whether to say no, and an end user's browser
    holding this cookie at all is already the fact this endpoint cares
    about — whether that particular session has since expired changes
    nothing about whether it belongs here.
    """
    if end_user_session is not None:
        raise console_forbidden()
