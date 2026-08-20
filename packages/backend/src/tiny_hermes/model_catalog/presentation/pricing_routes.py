"""What an endpoint charges, entered and read back.

Amounts cross this boundary as **strings**. A JSON number would be a float
before anything here could object, and the value that got stored would not be
the value that was typed — which is the one mistake a module about money must
not make on its way in.

There is no update route. §12.4 needs the old price to survive so Runs created
under it stay measured by it, so entering a new one adds a version.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, Request, status
from pydantic import BaseModel, Field

from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.identity.application.auth_service import AuthService
from tiny_hermes.identity.domain.models import AuthenticatedUser
from tiny_hermes.identity.presentation.dependencies import (
    SESSION_COOKIE,
    authenticate_browser_user,
    forbidden,
    verify_browser_write,
)
from tiny_hermes.model_catalog.application.pricing_service import (
    ForbiddenPricingAction,
    InvalidPrice,
    PricingService,
    PricingVersion,
    UnknownEndpoint,
)
from tiny_hermes.shared.errors import AppError
from tiny_hermes.tenancy.domain.models import Actor

CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token")]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]

#: A decimal as text: digits, an optional point, digits. Deliberately not a
#: float field — see the module docstring.
AMOUNT = r"^\d{1,12}(\.\d{1,6})?$"


class SetPriceRequest(BaseModel):
    currency: str = Field(min_length=3, max_length=3)
    input_per_million: str = Field(pattern=AMOUNT)
    output_per_million: str = Field(pattern=AMOUNT)
    cached_input_per_million: str | None = Field(default=None, pattern=AMOUNT)
    #: When this price started applying. Defaults to now; a future value is
    #: stored and not used until it arrives.
    effective_at: datetime | None = None


class PricingVersionResponse(BaseModel):
    id: UUID
    endpoint_id: UUID
    version_number: int
    currency: str
    input_per_million: str
    output_per_million: str
    cached_input_per_million: str | None
    #: True when an administrator declared this endpoint free. Sent as its own
    #: field so a console never has to infer it from two zeroes — "priced at
    #: nothing" and "not priced" are different states and this is the one that
    #: is a price.
    free: bool
    effective_at: datetime
    created_by: UUID
    created_at: datetime

    @classmethod
    def from_domain(cls, version: PricingVersion) -> "PricingVersionResponse":
        prices = version.prices
        return cls(
            id=version.id,
            endpoint_id=version.endpoint_id,
            version_number=version.version_number,
            currency=prices.currency,
            input_per_million=str(prices.input_per_million),
            output_per_million=str(prices.output_per_million),
            cached_input_per_million=(
                None
                if prices.cached_input_per_million is None
                else str(prices.cached_input_per_million)
            ),
            free=prices.is_free,
            effective_at=version.effective_at,
            created_by=version.created_by,
            created_at=version.created_at,
        )


def pricing_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/model-endpoints", tags=["model-endpoints"])
    auth_dependency = resources.auth_service
    service_dependency = resources.pricing_service

    @router.get(
        "/{endpoint_id}/pricing", response_model=list[PricingVersionResponse]
    )
    async def list_pricing(  # pyright: ignore[reportUnusedFunction]
        endpoint_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        service: Annotated[
            PricingService, Depends(service_dependency, scope="function")
        ],
        session_token: SessionCookie = None,
    ) -> list[PricingVersionResponse]:
        user = await authenticate_browser_user(auth, session_token)
        try:
            listed = await service.list_versions(
                _actor(user), endpoint_id, request.state.request_id
            )
        except ForbiddenPricingAction as error:
            raise forbidden() from error
        except UnknownEndpoint as error:
            raise _not_found() from error
        return [PricingVersionResponse.from_domain(item) for item in listed]

    @router.post(
        "/{endpoint_id}/pricing",
        response_model=PricingVersionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def set_price(  # pyright: ignore[reportUnusedFunction]
        endpoint_id: UUID,
        payload: SetPriceRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        service: Annotated[
            PricingService, Depends(service_dependency, scope="function")
        ],
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> PricingVersionResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        try:
            version = await service.set_price(
                _actor(user),
                endpoint_id,
                currency=payload.currency,
                input_per_million=payload.input_per_million,
                output_per_million=payload.output_per_million,
                cached_input_per_million=payload.cached_input_per_million,
                effective_at=payload.effective_at,
                request_id=request.state.request_id,
            )
        except ForbiddenPricingAction as error:
            raise forbidden() from error
        except UnknownEndpoint as error:
            raise _not_found() from error
        except InvalidPrice as error:
            raise AppError(
                code="invalid_price",
                title="Invalid price",
                status=422,
                detail=error.reason,
            ) from error
        return PricingVersionResponse.from_domain(version)

    return router


def _actor(user: AuthenticatedUser) -> Actor:
    return Actor(user.id, user.is_platform_admin)


def _not_found() -> AppError:
    return AppError(
        code="model_endpoint_not_found",
        title="Endpoint not found",
        status=404,
        detail="No model endpoint by that identifier is registered.",
    )
