"""Design §3 (issuer registry) and §4.2-4.3 (credential exchange, session
revocation). Two audiences share this router: a workspace admin manages
`channel_issuers` the same way they manage service accounts, and an end user
— who never signs in to this platform at all — exchanges an enterprise-signed
credential for the one thing this platform ever gives them: a session cookie.

Every route here says up front which of those two it serves, per the global
constraint that a new endpoint must never leave that ambiguous. The
credential-exchange and revocation shapes are the brief's own — a path or a
cookie attribute changed here is a contract changed with every enterprise
that already integrated against it.
"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, Request, Response, status
from pydantic import BaseModel, Field

from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.identity.application.auth_service import AuthService
from tiny_hermes.identity.application.end_user_service import (
    ChannelIssuerRecord,
    CredentialExchangeRefused,
    EndUserIdentityService,
    ForbiddenEndUserAction,
    InvalidChannelIssuer,
    UnknownChannelIssuer,
    UnknownEndUser,
)
from tiny_hermes.identity.domain.end_user_credential import RefusalReason
from tiny_hermes.identity.domain.models import AuthenticatedUser
from tiny_hermes.identity.ports.end_user_store import IssuerAlreadyRegistered
from tiny_hermes.identity.presentation.dependencies import (
    SESSION_COOKIE,
    authenticate_browser_user,
    forbidden,
    parse_bearer_token,
    require_workspace_id,
    unauthenticated_bearer,
    verify_browser_write,
)
from tiny_hermes.identity.presentation.end_user_dependencies import END_USER_SESSION_COOKIE
from tiny_hermes.shared.errors import AppError
from tiny_hermes.tenancy.domain.models import Actor

WorkspaceHeader = Annotated[str | None, Header(alias="X-Workspace-Id")]
CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token")]
AuthorizationHeader = Annotated[str | None, Header(alias="Authorization")]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


class RegisterChannelIssuerRequest(BaseModel):
    channel: str = Field(min_length=1, max_length=32)
    issuer: str = Field(min_length=1, max_length=255)
    public_key: str | None = None
    jwks_url: str | None = Field(default=None, max_length=2048)
    allowed_origins: list[str] = Field(default_factory=list)


class ChannelIssuerResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    channel: str
    issuer: str
    public_key: str | None
    jwks_url: str | None
    allowed_origins: list[str]
    status: str
    created_by: UUID
    created_at: datetime

    @classmethod
    def from_domain(cls, record: ChannelIssuerRecord) -> "ChannelIssuerResponse":
        return cls(
            id=record.id,
            workspace_id=record.workspace_id,
            channel=record.channel,
            issuer=record.issuer,
            public_key=record.public_key,
            jwks_url=record.jwks_url,
            allowed_origins=list(record.allowed_origins),
            status=record.status.value,
            created_by=record.created_by,
            created_at=record.created_at,
        )


class EndUserSessionResponse(BaseModel):
    end_user_id: UUID
    expires_at: datetime


def end_user_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["end-user-identity"])
    auth_dependency = resources.auth_service
    end_users_dependency = resources.end_user_identity_service

    # -- channel_issuers: a workspace admin's console capability -----------

    @router.post(
        "/channel-issuers",
        response_model=ChannelIssuerResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def register_issuer(  # pyright: ignore[reportUnusedFunction]
        payload: RegisterChannelIssuerRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        end_users: Annotated[
            EndUserIdentityService, Depends(end_users_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> ChannelIssuerResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            record = await end_users.register_issuer(
                _actor(user),
                workspace_id,
                channel=payload.channel,
                issuer=payload.issuer,
                public_key=payload.public_key,
                jwks_url=payload.jwks_url,
                allowed_origins=payload.allowed_origins,
                request_id=request.state.request_id,
            )
        except ForbiddenEndUserAction as error:
            raise forbidden() from error
        except InvalidChannelIssuer as error:
            raise AppError(
                code="invalid_channel_issuer",
                title="Invalid channel issuer",
                status=422,
                detail=error.reason,
            ) from error
        except IssuerAlreadyRegistered as error:
            raise AppError(
                code="channel_issuer_already_registered",
                title="Channel issuer already registered",
                status=409,
                detail="That channel already has an issuer by this name in this workspace.",
            ) from error
        return ChannelIssuerResponse.from_domain(record)

    @router.get("/channel-issuers", response_model=list[ChannelIssuerResponse])
    async def list_issuers(  # pyright: ignore[reportUnusedFunction]
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        end_users: Annotated[
            EndUserIdentityService, Depends(end_users_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> list[ChannelIssuerResponse]:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            records = await end_users.list_issuers(_actor(user), workspace_id)
        except ForbiddenEndUserAction as error:
            raise forbidden() from error
        return [ChannelIssuerResponse.from_domain(record) for record in records]

    @router.post("/channel-issuers/{issuer_id}/disable", response_model=ChannelIssuerResponse)
    async def disable_issuer(  # pyright: ignore[reportUnusedFunction]
        issuer_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        end_users: Annotated[
            EndUserIdentityService, Depends(end_users_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> ChannelIssuerResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            record = await end_users.disable_issuer(
                _actor(user), workspace_id, issuer_id, request.state.request_id
            )
        except ForbiddenEndUserAction as error:
            raise forbidden() from error
        except UnknownChannelIssuer as error:
            raise _unknown_issuer() from error
        return ChannelIssuerResponse.from_domain(record)

    # -- the end user's own entry point, design §4.2-4.3 --------------------

    @router.post(
        "/end-user/sessions",
        response_model=EndUserSessionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def exchange_session(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        response: Response,
        end_users: Annotated[
            EndUserIdentityService, Depends(end_users_dependency, scope="function")
        ],
        authorization: AuthorizationHeader = None,
        selected_workspace: WorkspaceHeader = None,
    ) -> EndUserSessionResponse:
        token = parse_bearer_token(authorization)
        if token is None:
            raise unauthenticated_bearer()
        workspace_id = require_workspace_id(selected_workspace)
        try:
            exchanged = await end_users.exchange(
                token, workspace_id, datetime.now(UTC), request.state.request_id
            )
        except CredentialExchangeRefused as error:
            raise _refusal(error.reason) from error
        # HttpOnly, SameSite=None, Secure, unconditionally (design §4.2):
        # this cookie is always read cross-origin, embedded in an
        # enterprise's own page, so there is no "development" exemption the
        # platform-member cookie in `identity/presentation/routes.py` takes.
        response.set_cookie(
            END_USER_SESSION_COOKIE,
            exchanged.session_token,
            max_age=resources.settings.end_user_session_ttl_seconds,
            httponly=True,
            secure=True,
            samesite="none",
            path="/",
        )
        return EndUserSessionResponse(
            end_user_id=exchanged.end_user_id, expires_at=exchanged.expires_at
        )

    @router.delete("/end-user/sessions/{end_user_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def revoke_end_user_sessions(  # pyright: ignore[reportUnusedFunction]
        end_user_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        end_users: Annotated[
            EndUserIdentityService, Depends(end_users_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> None:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            await end_users.revoke_sessions(
                _actor(user),
                workspace_id,
                end_user_id,
                request.state.request_id,
                datetime.now(UTC),
            )
        except ForbiddenEndUserAction as error:
            raise forbidden() from error
        except UnknownEndUser as error:
            raise AppError(
                code="end_user_not_found",
                title="End user not found",
                status=404,
                detail="No end user by that identifier is available in this workspace.",
            ) from error

    return router


def _actor(user: AuthenticatedUser) -> Actor:
    return Actor(user.id, user.is_platform_admin)


def _unknown_issuer() -> AppError:
    return AppError(
        code="channel_issuer_not_found",
        title="Channel issuer not found",
        status=404,
        detail="No channel issuer by that identifier is available in this workspace.",
    )


def _refusal(reason: RefusalReason) -> AppError:
    if reason is RefusalReason.LIFETIME_EXCEEDS_PLATFORM_CEILING:
        # design §8: the one refusal worth naming — an enterprise's signer
        # asked for longer than the platform's 15-minute ceiling allows, and
        # that is their own misconfiguration, not something an attacker
        # learns by probing.
        return AppError(
            code="end_user_credential_lifetime_exceeds_ceiling",
            title="Credential lifetime exceeds the platform ceiling",
            status=401,
            detail="The credential's exp is more than 15 minutes from now.",
        )
    return AppError(
        code="end_user_credential_invalid",
        title="Invalid credential",
        status=401,
        detail="The credential could not be verified.",
    )
