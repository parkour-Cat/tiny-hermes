"""Who may say what a model costs.

Only a platform administrator. An endpoint is platform-level and so is its
price: a workspace that could set one would be deciding what every other
workspace's Runs are measured at, and a service account that could set one
would make the whole of §12.4 something a key can rewrite.

The other thing pinned here is that there is no way to *edit* a price. Entering
a new one adds a version, because Runs created under the old one are still
measured by it — which is why this service has no update method to reach for
and why a test asserts the old row is still there afterwards.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from tiny_hermes.model_catalog.application.pricing_service import (
    ForbiddenPricingAction,
    InvalidPrice,
    PricingService,
    PricingVersion,
    UnknownEndpoint,
)
from tiny_hermes.model_catalog.domain.pricing import TokenPrices
from tiny_hermes.tenancy.domain.models import Actor, Role

ENDPOINT = uuid4()


@dataclass
class Store:
    versions: list[PricingVersion] = field(default_factory=list[PricingVersion])
    audit: list[str] = field(default_factory=list[str])
    known: bool = True

    async def endpoint_exists(self, endpoint_id: UUID) -> bool:
        del endpoint_id
        return self.known

    async def add_version(
        self,
        *,
        endpoint_id: UUID,
        prices: TokenPrices,
        effective_at: datetime,
        created_by: UUID,
    ) -> PricingVersion:
        version = PricingVersion(
            id=uuid4(),
            endpoint_id=endpoint_id,
            version_number=len(self.versions) + 1,
            prices=prices,
            effective_at=effective_at,
            created_by=created_by,
            created_at=datetime.now(UTC),
        )
        self.versions.append(version)
        return version

    async def list_versions(self, endpoint_id: UUID) -> Sequence[PricingVersion]:
        del endpoint_id
        return list(self.versions)

    async def current_version(self, endpoint_id: UUID) -> PricingVersion | None:
        del endpoint_id
        return self.versions[-1] if self.versions else None

    async def get_version(self, version_id: UUID) -> PricingVersion | None:
        return next((item for item in self.versions if item.id == version_id), None)

    async def append_audit(
        self,
        *,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
        request_id: str,
        context: dict[str, str] | None = None,
    ) -> None:
        del actor_id, resource_id, request_id
        self.audit.append(f"{action}:{(context or {}).get('free')}")


def service(store: Store | None = None) -> tuple[PricingService, Store]:
    kept = store or Store()
    return PricingService(kept), kept


async def price(
    catalog: PricingService,
    actor: Actor,
    *,
    input_price: str = "3",
    output_price: str = "15",
    effective_at: datetime | None = None,
) -> PricingVersion:
    return await catalog.set_price(
        actor,
        ENDPOINT,
        currency="USD",
        input_per_million=input_price,
        output_per_million=output_price,
        cached_input_per_million=None,
        effective_at=effective_at,
        request_id="req-1",
    )


PLATFORM_ADMIN = Actor(uuid4(), True)
MEMBER = Actor(uuid4(), False)
MACHINE = Actor(uuid4(), False, role=Role.WORKSPACE_ADMIN, is_service_account=True)


# -- who may ----------------------------------------------------------------


async def test_a_platform_administrator_may_price_an_endpoint() -> None:
    catalog, store = service()

    version = await price(catalog, PLATFORM_ADMIN)

    assert version.version_number == 1
    assert store.versions[0].prices.input_per_million == Decimal("3")


async def test_a_workspace_member_may_not() -> None:
    catalog, _ = service()

    with pytest.raises(ForbiddenPricingAction):
        await price(catalog, MEMBER)


async def test_a_service_account_may_not_even_as_a_workspace_admin() -> None:
    """A key that could price would make §12.4 something a key can rewrite."""
    catalog, _ = service()

    with pytest.raises(ForbiddenPricingAction):
        await price(catalog, MACHINE)


async def test_a_member_may_not_read_the_prices_either() -> None:
    catalog, _ = service()

    with pytest.raises(ForbiddenPricingAction):
        await catalog.list_versions(MEMBER, ENDPOINT, "req-1")


async def test_pricing_an_endpoint_that_does_not_exist_is_not_found() -> None:
    catalog, _ = service(Store(known=False))

    with pytest.raises(UnknownEndpoint):
        await price(catalog, PLATFORM_ADMIN)


# -- what a price may be ----------------------------------------------------


@pytest.mark.parametrize("amount", ["not a number", "", "NaN", "Infinity"])
async def test_an_amount_that_is_not_a_finite_number_is_refused(amount: str) -> None:
    catalog, _ = service()

    with pytest.raises(InvalidPrice):
        await price(catalog, PLATFORM_ADMIN, input_price=amount)


async def test_a_negative_amount_is_refused() -> None:
    catalog, _ = service()

    with pytest.raises(InvalidPrice):
        await price(catalog, PLATFORM_ADMIN, input_price="-1")


async def test_a_currency_that_is_not_one_is_refused() -> None:
    catalog, _ = service()

    with pytest.raises(InvalidPrice):
        await catalog.set_price(
            PLATFORM_ADMIN,
            ENDPOINT,
            currency="dollars",
            input_per_million="3",
            output_per_million="15",
            cached_input_per_million=None,
            effective_at=None,
            request_id="req-1",
        )


async def test_an_amount_keeps_the_precision_it_was_typed_with() -> None:
    """Parsed with `Decimal` from a string. A float on the way in would round
    before anything here could object."""
    catalog, _ = service()

    version = await price(catalog, PLATFORM_ADMIN, input_price="0.000003")

    assert version.prices.input_per_million == Decimal("0.000003")


# -- versions rather than edits ---------------------------------------------


async def test_a_second_price_does_not_replace_the_first() -> None:
    catalog, store = service()
    await price(catalog, PLATFORM_ADMIN, input_price="3")

    await price(catalog, PLATFORM_ADMIN, input_price="4")

    assert [item.prices.input_per_million for item in store.versions] == [
        Decimal("3"),
        Decimal("4"),
    ]


async def test_a_future_price_is_stored_with_the_moment_it_starts() -> None:
    catalog, _ = service()
    monday = datetime.now(UTC) + timedelta(days=3)

    version = await price(catalog, PLATFORM_ADMIN, effective_at=monday)

    assert version.effective_at == monday


async def test_declaring_an_endpoint_free_is_recorded_as_a_decision() -> None:
    """"Somebody declared this free" is exactly the decision a reader will want
    to find later, and it is indistinguishable from "nobody priced it"
    anywhere else."""
    catalog, store = service()

    await price(catalog, PLATFORM_ADMIN, input_price="0", output_price="0")

    assert store.audit == ["model_endpoint.priced:true"]


async def test_an_ordinary_price_is_recorded_as_not_free() -> None:
    catalog, store = service()

    await price(catalog, PLATFORM_ADMIN)

    assert store.audit == ["model_endpoint.priced:false"]
