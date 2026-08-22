from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.identity.application.auth_service import AuthService
from tiny_hermes.identity.application.oidc_service import (
    ForbiddenOidcAction,
    InvalidOidcProvider,
    OidcLoginRefused,
    OidcProviderNotUsable,
    OidcProviderService,
    OidcProviderUnreachable,
    UnknownOidcProvider,
)
from tiny_hermes.identity.domain.models import AuthenticatedUser
from tiny_hermes.identity.ports.oidc_store import OidcProviderRecord
from tiny_hermes.identity.presentation.dependencies import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    authenticate_browser_user,
    forbidden,
    verify_browser_write,
)
from tiny_hermes.identity.presentation.routes import UserResponse
from tiny_hermes.shared.errors import AppError
from tiny_hermes.tenancy.domain.models import Actor

WorkspaceHeader = Annotated[str | None, Header(alias="X-Workspace-Id")]
CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token")]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


class RegisterOidcProviderRequest(BaseModel):
    issuer: str = Field(min_length=1, max_length=500)
    client_id: str = Field(min_length=1, max_length=255)
    #: An environment variable name or the id of an active platform `Secret`
    #: — never the plaintext client secret. See `OidcProviderRow`'s own
    #: docstring; this is the same two-shape reference
    #: `ModelEndpointRow.credential_ref` already uses.
    client_secret_ref: str = Field(min_length=1, max_length=200)
    discovery_url: str = Field(min_length=1, max_length=500)
    scopes: list[str] = Field(default_factory=list)


class OfferableProviderResponse(BaseModel):
    """What an anonymous login page is told about an identity provider:
    an id to start the flow with, and the issuer to put on the button.

    Its own model rather than a subset of `OidcProviderResponse`, so that
    adding a field there — a client id, a discovery URL, whatever a future
    admin screen wants — cannot leak it here by inheritance. That this
    reveals which IdPs the deployment trusts is unavoidable and accepted:
    a login page cannot offer a choice it refuses to name.
    """

    id: UUID
    issuer: str


class OidcProviderResponse(BaseModel):
    id: UUID
    issuer: str
    client_id: str
    #: The reference, not the secret it names — see the request model's own
    #: field docstring.
    client_secret_ref: str
    discovery_url: str
    scopes: list[str]
    status: str
    created_by: UUID
    created_at: datetime

    @classmethod
    def from_domain(cls, record: OidcProviderRecord) -> "OidcProviderResponse":
        return cls(
            id=record.id,
            issuer=record.issuer,
            client_id=record.client_id,
            client_secret_ref=record.client_secret_ref,
            discovery_url=record.discovery_url,
            scopes=list(record.scopes),
            status=record.status.value,
            created_by=record.created_by,
            created_at=record.created_at,
        )


def oidc_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["oidc"])
    auth_dependency = resources.auth_service
    oidc_dependency = resources.oidc_provider_service

    # -- §1: provider configuration (platform admin only) -------------------

    @router.post(
        "/oidc/providers", response_model=OidcProviderResponse, status_code=status.HTTP_201_CREATED
    )
    async def register_provider(  # pyright: ignore[reportUnusedFunction]
        payload: RegisterOidcProviderRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        oidc: Annotated[OidcProviderService, Depends(oidc_dependency, scope="function")],
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> OidcProviderResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        try:
            record = await oidc.register(
                _actor(user),
                issuer=payload.issuer,
                client_id=payload.client_id,
                client_secret_ref=payload.client_secret_ref,
                discovery_url=payload.discovery_url,
                scopes=payload.scopes,
                request_id=request.state.request_id,
            )
        except ForbiddenOidcAction as error:
            raise forbidden() from error
        except InvalidOidcProvider as error:
            raise AppError(
                code="invalid_oidc_provider",
                title="Invalid OIDC provider",
                status=422,
                detail=str(error),
            ) from error
        return OidcProviderResponse.from_domain(record)

    @router.get("/auth/oidc/available", response_model=list[OfferableProviderResponse])
    async def available_providers(  # pyright: ignore[reportUnusedFunction]
        oidc: Annotated[OidcProviderService, Depends(oidc_dependency, scope="function")],
    ) -> list[OfferableProviderResponse]:
        """Unauthenticated on purpose: the login page has to render before
        anyone has signed in, so an authenticated list could never feed it.

        No `Depends` on auth at all, rather than an optional one — an
        endpoint that sometimes reads a session invites a later change that
        widens what it returns for a signed-in caller, and this one must
        return the same thin shape to everybody.
        """
        return [
            OfferableProviderResponse(id=record.id, issuer=record.issuer)
            for record in await oidc.offerable_providers()
        ]

    @router.get("/oidc/providers", response_model=list[OidcProviderResponse])
    async def list_providers(  # pyright: ignore[reportUnusedFunction]
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        oidc: Annotated[OidcProviderService, Depends(oidc_dependency, scope="function")],
        session_token: SessionCookie = None,
    ) -> list[OidcProviderResponse]:
        user = await authenticate_browser_user(auth, session_token)
        try:
            records = await oidc.list_providers(_actor(user))
        except ForbiddenOidcAction as error:
            raise forbidden() from error
        return [OidcProviderResponse.from_domain(record) for record in records]

    @router.post("/oidc/providers/{provider_id}/disable", response_model=OidcProviderResponse)
    async def disable_provider(  # pyright: ignore[reportUnusedFunction]
        provider_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        oidc: Annotated[OidcProviderService, Depends(oidc_dependency, scope="function")],
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> OidcProviderResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        try:
            record = await oidc.disable(_actor(user), provider_id, request.state.request_id)
        except ForbiddenOidcAction as error:
            raise forbidden() from error
        except UnknownOidcProvider as error:
            raise AppError(
                code="oidc_provider_not_found",
                title="OIDC provider not found",
                status=404,
                detail="No OIDC provider by that identifier is registered.",
            ) from error
        return OidcProviderResponse.from_domain(record)

    # -- §2: the flow (anonymous — this is how a session begins) -----------

    @router.get("/auth/oidc/{provider_id}/start", name="oidc_start")
    async def start(  # pyright: ignore[reportUnusedFunction]
        provider_id: UUID,
        request: Request,
        oidc: Annotated[OidcProviderService, Depends(oidc_dependency, scope="function")],
    ) -> RedirectResponse:
        # Named so `callback` below can rebuild the identical URL from the
        # same route — a redirect_uri that drifted between `/start` and
        # `/callback` is a redirect_uri the IdP would (rightly) refuse.
        redirect_uri = str(request.url_for("oidc_callback", provider_id=provider_id))
        try:
            redirect = await oidc.start(provider_id, redirect_uri)
        except OidcProviderNotUsable as error:
            raise _provider_not_usable() from error
        except OidcProviderUnreachable as error:
            raise _provider_unreachable() from error
        return RedirectResponse(redirect.url, status_code=status.HTTP_302_FOUND)

    @router.get(
        "/auth/oidc/{provider_id}/callback", name="oidc_callback", response_model=UserResponse
    )
    async def callback(  # pyright: ignore[reportUnusedFunction]
        provider_id: UUID,
        request: Request,
        response: Response,
        oidc: Annotated[OidcProviderService, Depends(oidc_dependency, scope="function")],
        code: Annotated[str | None, Query()] = None,
        state: Annotated[str | None, Query()] = None,
    ) -> UserResponse:
        if not code or not state:
            raise _login_refused()
        try:
            result = await oidc.handle_callback(
                provider_id, code=code, state=state, request_id=request.state.request_id
            )
        except OidcProviderNotUsable as error:
            raise _provider_not_usable() from error
        except OidcProviderUnreachable as error:
            raise _provider_unreachable() from error
        except OidcLoginRefused as error:
            raise _login_refused() from error
        # The same cookie shape local login sets (`identity_router.login`) —
        # design §2's "与本地登录同一条路径". Where the browser goes *after*
        # this response is a console concern (§4), not this route's.
        secure = resources.settings.environment != "development"
        response.set_cookie(
            SESSION_COOKIE,
            result.session_token,
            max_age=resources.settings.session_ttl_seconds,
            httponly=True,
            secure=secure,
            samesite="lax",
            path="/",
        )
        response.set_cookie(
            CSRF_COOKIE,
            result.csrf_token,
            max_age=resources.settings.session_ttl_seconds,
            httponly=False,
            secure=secure,
            samesite="lax",
            path="/",
        )
        return UserResponse.from_domain(result.user)

    return router


def _actor(user: AuthenticatedUser) -> Actor:
    return Actor(user.id, user.is_platform_admin)


def _provider_not_usable() -> AppError:
    return AppError(
        code="oidc_provider_not_available",
        title="OIDC provider not available",
        status=404,
        detail="This OIDC provider is not configured or is disabled.",
    )


def _provider_unreachable() -> AppError:
    return AppError(
        code="oidc_provider_unreachable",
        title="OIDC provider unreachable",
        status=503,
        detail="The OIDC provider could not be reached.",
    )


def _login_refused() -> AppError:
    return AppError(
        code="oidc_login_failed",
        title="OIDC login failed",
        status=400,
        detail="The OIDC login could not be completed.",
    )
