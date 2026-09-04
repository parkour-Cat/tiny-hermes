from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, status
from pydantic import BaseModel, Field

from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.identity.application.auth_service import AuthService
from tiny_hermes.identity.domain.models import AuthenticatedUser
from tiny_hermes.identity.presentation.dependencies import (
    SESSION_COOKIE,
    authenticate_browser_user,
    verify_browser_write,
)
from tiny_hermes.model_catalog.application.service import CheckResult, ModelEndpointService
from tiny_hermes.model_catalog.domain.models import (
    EndpointStatus,
    ModelEndpoint,
    ModelEndpointSpec,
)
from tiny_hermes.tenancy.domain.models import Actor

CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token")]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


class UpdateEndpointRequest(BaseModel):
    """What may be changed after registration, which is deliberately little.

    `status` was the whole of it. `accepts_images` joins it because it is a
    *statement about* the endpoint — already true or already false — rather
    than a choice of endpoint. `model` and `base_url` stay out: changing
    either swaps the endpoint for a different one underneath every
    AgentVersion that named it, and that is a new registration.

    `context_window` and `max_output_tokens` join for the same reason: how
    much the endpoint holds is a fact about it, not a choice of it, and a
    Run reads its endpoint's window at the start of every round — so a
    window widened here reaches a Run paused at `context_overflow` the
    moment it is resumed. Before this, the only way to recover such a Run
    was a database session.

    Every field is optional and absent means unchanged, so a request naming
    one cannot silently reset another.
    """

    status: EndpointStatus | None = None
    accepts_images: bool | None = None
    context_window: int | None = Field(default=None, ge=1, le=10_000_000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=10_000_000)


class EndpointSummary(BaseModel):
    """What any signed-in user may see.

    No `base_url`: an internal model host is a piece of network map, and the
    draft editor has no use for it. No credential and no reference to one.
    """

    id: UUID
    name: str
    model: str
    context_window: int
    max_output_tokens: int
    usage_quality: str
    #: Both of these are budget planning facts, and they are on the summary
    #: rather than the detail because an Agent author choosing an endpoint is
    #: choosing how much conversation it can hold.
    context_accounting: str
    tokenizer: str | None
    #: Whether this endpoint takes image input. Carried out to callers
    #: because the console has to show it and let it be changed — a
    #: declaration nobody can read is one nobody can correct.
    accepts_images: bool
    status: str

    @classmethod
    def from_domain(cls, endpoint: ModelEndpoint) -> "EndpointSummary":
        return cls(
            id=endpoint.id,
            name=endpoint.spec.name,
            model=endpoint.spec.model,
            context_window=endpoint.spec.context_window,
            max_output_tokens=endpoint.spec.max_output_tokens,
            usage_quality=endpoint.spec.usage_quality.value,
            context_accounting=endpoint.spec.context_accounting.value,
            tokenizer=endpoint.spec.tokenizer,
            accepts_images=endpoint.spec.accepts_images,
            status=endpoint.status.value,
        )


class EndpointDetail(EndpointSummary):
    """What a platform administrator may see.

    `credential_available` rather than `credential_ref`: whether the deployment
    supplied the key is the fact an administrator needs, and naming the variable
    only tells a reader where to go looking.
    """

    kind: str
    base_url: str
    credential_available: bool

    @classmethod
    def detail_from(
        cls, endpoint: ModelEndpoint, credential_available: bool
    ) -> "EndpointDetail":
        summary = EndpointSummary.from_domain(endpoint)
        return cls(
            **summary.model_dump(),
            kind=endpoint.spec.kind,
            base_url=endpoint.spec.base_url,
            credential_available=credential_available,
        )


class CheckResponse(BaseModel):
    reachable: bool
    elapsed_ms: int
    refusal: str | None = None
    detail: str | None = None

    @classmethod
    def from_domain(cls, result: CheckResult) -> "CheckResponse":
        return cls(
            reachable=result.reachable,
            elapsed_ms=result.elapsed_ms,
            refusal=result.refusal,
            detail=result.detail,
        )


def model_endpoint_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/model-endpoints", tags=["model-endpoints"])
    auth_dependency = resources.auth_service
    endpoints_dependency = resources.model_endpoints

    @router.get("", response_model=list[EndpointSummary])
    async def list_endpoints(  # pyright: ignore[reportUnusedFunction]
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        endpoints: Annotated[ModelEndpointService, Depends(endpoints_dependency, scope="function")],
        session_token: SessionCookie = None,
    ) -> list[EndpointSummary]:
        await authenticate_browser_user(auth, session_token)
        return [
            EndpointSummary.from_domain(entry) for entry in await endpoints.list_selectable()
        ]

    @router.get("/{endpoint_id}", response_model=EndpointDetail)
    async def read_endpoint(  # pyright: ignore[reportUnusedFunction]
        endpoint_id: UUID,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        endpoints: Annotated[ModelEndpointService, Depends(endpoints_dependency, scope="function")],
        session_token: SessionCookie = None,
    ) -> EndpointDetail:
        user = await authenticate_browser_user(auth, session_token)
        endpoint = await endpoints.read(_actor(user), endpoint_id)
        return EndpointDetail.detail_from(
            endpoint, await endpoints.credential_available(endpoint)
        )

    @router.post("", response_model=EndpointDetail, status_code=status.HTTP_201_CREATED)
    async def register_endpoint(  # pyright: ignore[reportUnusedFunction]
        payload: ModelEndpointSpec,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        endpoints: Annotated[ModelEndpointService, Depends(endpoints_dependency, scope="function")],
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> EndpointDetail:
        user = await verify_browser_write(auth, session_token, csrf_token)
        endpoint = await endpoints.register(_actor(user), payload)
        return EndpointDetail.detail_from(
            endpoint, await endpoints.credential_available(endpoint)
        )

    @router.patch("/{endpoint_id}", response_model=EndpointDetail)
    async def update_endpoint(  # pyright: ignore[reportUnusedFunction]
        endpoint_id: UUID,
        payload: UpdateEndpointRequest,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        endpoints: Annotated[ModelEndpointService, Depends(endpoints_dependency, scope="function")],
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> EndpointDetail:
        user = await verify_browser_write(auth, session_token, csrf_token)
        updated = await endpoints.amend(
            _actor(user),
            endpoint_id,
            status=payload.status,
            accepts_images=payload.accepts_images,
            context_window=payload.context_window,
            max_output_tokens=payload.max_output_tokens,
        )
        return EndpointDetail.detail_from(
            updated, await endpoints.credential_available(updated)
        )

    @router.post("/{endpoint_id}/check", response_model=CheckResponse)
    async def check_endpoint(  # pyright: ignore[reportUnusedFunction]
        endpoint_id: UUID,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        endpoints: Annotated[ModelEndpointService, Depends(endpoints_dependency, scope="function")],
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> CheckResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        async with resources.outbound_client() as client:
            result = await endpoints.check(_actor(user), endpoint_id, client)
        return CheckResponse.from_domain(result)

    return router


def _actor(user: AuthenticatedUser) -> Actor:
    return Actor(user.id, user.is_platform_admin)
