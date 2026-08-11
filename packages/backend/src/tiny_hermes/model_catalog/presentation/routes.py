from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, status
from pydantic import BaseModel

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
from tiny_hermes.model_catalog.infrastructure import credentials
from tiny_hermes.tenancy.domain.models import Actor

CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token")]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


class UpdateStatusRequest(BaseModel):
    status: EndpointStatus


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
    def detail_from(cls, endpoint: ModelEndpoint) -> "EndpointDetail":
        summary = EndpointSummary.from_domain(endpoint)
        return cls(
            **summary.model_dump(),
            kind=endpoint.spec.kind,
            base_url=endpoint.spec.base_url,
            credential_available=credentials.is_available(endpoint.spec.credential_ref),
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
        return EndpointDetail.detail_from(await endpoints.read(_actor(user), endpoint_id))

    @router.post("", response_model=EndpointDetail, status_code=status.HTTP_201_CREATED)
    async def register_endpoint(  # pyright: ignore[reportUnusedFunction]
        payload: ModelEndpointSpec,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        endpoints: Annotated[ModelEndpointService, Depends(endpoints_dependency, scope="function")],
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> EndpointDetail:
        user = await verify_browser_write(auth, session_token, csrf_token)
        return EndpointDetail.detail_from(await endpoints.register(_actor(user), payload))

    @router.patch("/{endpoint_id}", response_model=EndpointDetail)
    async def update_status(  # pyright: ignore[reportUnusedFunction]
        endpoint_id: UUID,
        payload: UpdateStatusRequest,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        endpoints: Annotated[ModelEndpointService, Depends(endpoints_dependency, scope="function")],
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> EndpointDetail:
        user = await verify_browser_write(auth, session_token, csrf_token)
        updated = await endpoints.set_status(_actor(user), endpoint_id, payload.status)
        return EndpointDetail.detail_from(updated)

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
