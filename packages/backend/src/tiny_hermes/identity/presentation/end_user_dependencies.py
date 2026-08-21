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
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Cookie, Header
from starlette.datastructures import Headers

from tiny_hermes.identity.application.end_user_service import EndUserIdentityService
from tiny_hermes.identity.presentation.dependencies import SESSION_COOKIE
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
    #: §5's enterprise-side gate, carried from the session — see
    #: `EndUserSession.agents`. A route that starts a Run passes this
    #: straight to `AgentCatalog.resolve_end_user_agent` rather than reading
    #: it off anything the caller asserts on this particular request.
    agents: tuple[str, ...] = ()
    #: Which issuer minted this session (§7, task-7 review finding 3), or
    #: `None` for a session that predates the column — see
    #: `EndUserSession.channel_issuer_id`. `resolve_end_user_caller_for_write`
    #: passes this straight to `enforce_end_user_origin`.
    channel_issuer_id: UUID | None = None


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
    return EndUserCaller(
        session.end_user_id, session.workspace_id, session.agents, session.channel_issuer_id
    )


def cross_origin_forbidden() -> AppError:
    return AppError(
        code="end_user_origin_not_allowed",
        title="Origin not allowed",
        status=403,
        detail="This origin is not registered to embed this workspace's chat.",
    )


def _request_origin(headers: Headers) -> str | None:
    """`Origin` first, `Referer` as the fallback a form-style cross-site POST
    would still carry — design §7's own wording is "Origin／Referer", not
    just the first of the two. Neither header is trustworthy in the sense of
    proving who is asking; both are trustworthy in the sense that a browser
    sets them itself and a script cannot override them, which is the only
    property a same-origin check needs (the CSRF literature's usual
    argument for this exact pair)."""
    origin = headers.get("origin")
    if origin:
        return origin
    referer = headers.get("referer")
    if not referer:
        return None
    parsed = urlsplit(referer)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


async def enforce_end_user_origin(
    service: EndUserIdentityService,
    workspace_id: UUID,
    channel_issuer_id: UUID | None,
    headers: Headers,
) -> None:
    """Design §7's CSRF defence, and the reason it has to exist at all: the
    end-user session cookie is `SameSite=None; Secure` (§4.2, required for a
    cross-site embed) and end-user write routes carry no `X-CSRF-Token` — the
    design's own trade of that header away for "just carry the cookie". A
    hostile page can therefore make the browser send a state-changing
    request with the cookie attached; the only thing left to check is where
    the request came from, against the origins registered on the issuer
    that minted *this session's own credential* (`channel_issuer_id`) —
    never the workspace's whole `channel_issuers` set (task-7 review
    finding 3: that used to be a union across every active issuer, which let
    a second issuer's registered origin act on a session the first issuer
    minted).

    A request carrying neither header is let through rather than refused.
    That is not a loophole a forged cross-site request can use — a browser
    sends `Origin` on every cross-site `fetch`/`XHR`, regardless of
    `Referrer-Policy`, which is precisely the CSRF shape this check exists
    to close — it is what lets a same-origin request from a client that
    omits both (an older browser's simple form GET-turned-POST, a
    same-origin call under a strict `Referrer-Policy`) through unexamined,
    the same way a missing `X-CSRF-Token` never needed inventing evidence
    for on this codebase's console side either.
    """
    origin = _request_origin(headers)
    if origin is None:
        return
    allowed = await service.allowed_origins_for_issuer(workspace_id, channel_issuer_id)
    if origin not in allowed:
        raise cross_origin_forbidden()


async def resolve_end_user_caller_for_write(
    service: EndUserIdentityService, session_token: str | None, headers: Headers
) -> EndUserCaller:
    """Every state-changing end-user route's front door. Authentication
    first, origin second — refusing an unauthenticated request for its
    origin before its cookie is even checked would let a probe learn which
    origins a workspace has registered without ever holding a valid session,
    the same enumeration risk design §8 already rules out for issuer
    lookups.
    """
    caller = await resolve_end_user_caller(service, session_token)
    await enforce_end_user_origin(service, caller.workspace_id, caller.channel_issuer_id, headers)
    return caller


def reject_end_user_caller(
    end_user_session: EndUserSessionCookie = None,
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> None:
    """§8's last row, made into something every console router can depend on
    without knowing this feature exists. Presence alone is enough to refuse:
    checking validity would mean a database round trip on every console
    request just to decide whether to say no, and an end user's browser
    holding this cookie at all is already the fact this endpoint cares
    about — whether that particular session has since expired changes
    nothing about whether it belongs here.

    Task-9 review finding G: presence alone used to mean the end-user
    cookie's presence *by itself*, which 403'd a single-domain deployment's
    own case of "both a workspace member and an end user" — a browser
    attaches every cookie it holds for this origin to every request,
    regardless of which identity that request is actually trying to use, so
    a console call from that browser always carried the end-user cookie too.
    What changed: the end-user cookie only disqualifies a request that
    carries *no console credential of its own* — `session_token`/
    `authorization` — sitting next to it. Still presence, not validity, on
    both sides, so this is still a database-free decision either way; a
    forged or expired console cookie/bearer earns no free pass from this
    function, it just clears this one and meets
    `resolve_workspace_caller`'s own checks a moment later, exactly as it
    always would have on a request with no end-user cookie in it at all.
    """
    if end_user_session is not None and session_token is None and authorization is None:
        raise console_forbidden()
