"""Who may say what a model costs, and what happens to the old answer.

Only a platform administrator: an endpoint is platform-level and so is its
price, and a workspace that could set one would be a workspace deciding what
every other workspace's Runs are measured at.

**A price is never edited.** Entering a new one adds a version; the old one
stays because Runs created under it are still measured by it. That is §12.4's
whole point — a correction entered today must not rewrite what yesterday cost —
and it is why this service has no update method to reach for.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol
from uuid import UUID

from tiny_hermes.model_catalog.domain.pricing import (
    InvalidPricing,
    TokenPrices,
)
from tiny_hermes.tenancy.domain.models import Actor


@dataclass(frozen=True)
class PricingVersion:
    id: UUID
    endpoint_id: UUID
    version_number: int
    prices: TokenPrices
    effective_at: datetime
    created_by: UUID
    created_at: datetime


class PricingStore(Protocol):
    async def add_version(
        self,
        *,
        endpoint_id: UUID,
        prices: TokenPrices,
        effective_at: datetime,
        created_by: UUID,
    ) -> PricingVersion: ...

    async def list_versions(self, endpoint_id: UUID) -> Sequence[PricingVersion]: ...

    async def current_version(self, endpoint_id: UUID) -> PricingVersion | None: ...

    async def get_version(self, version_id: UUID) -> PricingVersion | None: ...

    async def endpoint_exists(self, endpoint_id: UUID) -> bool: ...

    async def append_audit(
        self,
        *,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
        request_id: str,
        context: dict[str, str] | None = None,
    ) -> None: ...


class PricingError(Exception):
    """Base for every expected refusal here."""


class ForbiddenPricingAction(PricingError):
    pass


class UnknownEndpoint(PricingError):
    pass


class InvalidPrice(PricingError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PricingService:
    store: PricingStore

    async def list_versions(
        self, actor: Actor, endpoint_id: UUID, request_id: str
    ) -> Sequence[PricingVersion]:
        del request_id
        _require_platform_admin(actor)
        if not await self.store.endpoint_exists(endpoint_id):
            raise UnknownEndpoint
        return await self.store.list_versions(endpoint_id)

    async def set_price(
        self,
        actor: Actor,
        endpoint_id: UUID,
        *,
        currency: str,
        input_per_million: str,
        output_per_million: str,
        cached_input_per_million: str | None,
        effective_at: datetime | None,
        request_id: str,
    ) -> PricingVersion:
        """Record what this endpoint charges, as a new version.

        The numbers arrive as strings and are parsed with `Decimal`. A float on
        the way in would round before anything here could object, and the value
        that got stored would not be the value that was typed.
        """
        _require_platform_admin(actor)
        if not await self.store.endpoint_exists(endpoint_id):
            raise UnknownEndpoint
        try:
            prices = TokenPrices(
                currency=currency,
                input_per_million=_amount("input_per_million", input_per_million),
                output_per_million=_amount("output_per_million", output_per_million),
                cached_input_per_million=(
                    None
                    if cached_input_per_million is None
                    else _amount(
                        "cached_input_per_million", cached_input_per_million
                    )
                ),
            )
        except InvalidPricing as refused:
            raise InvalidPrice(str(refused)) from refused
        version = await self.store.add_version(
            endpoint_id=endpoint_id,
            prices=prices,
            effective_at=effective_at or datetime.now(UTC),
            created_by=actor.id,
        )
        await self.store.append_audit(
            actor_id=actor.id,
            action="model_endpoint.priced",
            resource_id=endpoint_id,
            request_id=request_id,
            context={
                "currency": prices.currency,
                "version": str(version.version_number),
                # Recorded because "somebody declared this free" is exactly the
                # decision a reader will want to find later, and it is
                # indistinguishable from "nobody priced it" anywhere else.
                "free": "true" if prices.is_free else "false",
            },
        )
        return version


def _require_platform_admin(actor: Actor) -> None:
    if actor.is_service_account or not actor.is_platform_admin:
        raise ForbiddenPricingAction


def _amount(name: str, value: str) -> Decimal:
    try:
        parsed = Decimal(value.strip())
    except (InvalidOperation, AttributeError) as broken:
        raise InvalidPrice(f"{name} is not a number: {value!r}") from broken
    if not parsed.is_finite():
        raise InvalidPrice(f"{name} is not a finite number")
    return parsed
