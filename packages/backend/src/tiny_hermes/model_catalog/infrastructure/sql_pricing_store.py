"""Prices, read and written. Nothing here decides who may.

`current_version` is the one query with a rule in it: the newest version whose
`effective_at` has passed. A price entered today as effective next Monday is
therefore visible to an administrator and not yet used by a Run — which is what
`effective_at` is for.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.model_catalog.application.pricing_service import PricingVersion
from tiny_hermes.model_catalog.domain.pricing import TokenPrices
from tiny_hermes.model_catalog.infrastructure.pricing_tables import (
    ModelPricingVersionRow,
)
from tiny_hermes.model_catalog.infrastructure.tables import ModelEndpointRow


class SqlPricingStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def endpoint_exists(self, endpoint_id: UUID) -> bool:
        return (
            await self._session.scalar(
                select(ModelEndpointRow.id).where(ModelEndpointRow.id == endpoint_id)
            )
        ) is not None

    async def add_version(
        self,
        *,
        endpoint_id: UUID,
        prices: TokenPrices,
        effective_at: datetime,
        created_by: UUID,
    ) -> PricingVersion:
        highest = await self._session.scalar(
            select(func.max(ModelPricingVersionRow.version_number)).where(
                ModelPricingVersionRow.endpoint_id == endpoint_id
            )
        )
        row = ModelPricingVersionRow(
            id=uuid4(),
            endpoint_id=endpoint_id,
            version_number=int(highest or 0) + 1,
            currency=prices.currency,
            input_per_million=prices.input_per_million,
            output_per_million=prices.output_per_million,
            cached_input_per_million=prices.cached_input_per_million,
            effective_at=effective_at,
            created_by=created_by,
        )
        self._session.add(row)
        await self._session.flush()
        return _version(row)

    async def list_versions(self, endpoint_id: UUID) -> Sequence[PricingVersion]:
        rows = (
            await self._session.scalars(
                select(ModelPricingVersionRow)
                .where(ModelPricingVersionRow.endpoint_id == endpoint_id)
                .order_by(ModelPricingVersionRow.version_number)
            )
        ).all()
        return [_version(row) for row in rows]

    async def current_version(self, endpoint_id: UUID) -> PricingVersion | None:
        """The price in force right now, or `None` when there is none.

        `None` is what makes a Run's cost unknown for the rest of its life, so
        it is deliberately not a fallback to the newest row: a price scheduled
        for next Monday is not the price today.
        """
        row = await self._session.scalar(
            select(ModelPricingVersionRow)
            .where(
                ModelPricingVersionRow.endpoint_id == endpoint_id,
                ModelPricingVersionRow.effective_at <= datetime.now(UTC),
            )
            .order_by(
                ModelPricingVersionRow.effective_at.desc(),
                ModelPricingVersionRow.version_number.desc(),
            )
            .limit(1)
        )
        return None if row is None else _version(row)

    async def get_version(self, version_id: UUID) -> PricingVersion | None:
        row = await self._session.get(ModelPricingVersionRow, version_id)
        return None if row is None else _version(row)

    async def append_audit(
        self,
        *,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
        request_id: str,
        context: dict[str, str] | None = None,
    ) -> None:
        self._session.add(
            AuditEventRow(
                id=uuid4(),
                workspace_id=None,
                actor_type="user",
                actor_id=actor_id,
                action=action,
                resource_type="model_endpoint",
                resource_id=resource_id,
                result="succeeded",
                request_id=request_id,
                context=context or {},
            )
        )
        await self._session.flush()


def _version(row: ModelPricingVersionRow) -> PricingVersion:
    return PricingVersion(
        id=row.id,
        endpoint_id=row.endpoint_id,
        version_number=row.version_number,
        prices=TokenPrices(
            currency=row.currency,
            input_per_million=row.input_per_million,
            output_per_million=row.output_per_million,
            cached_input_per_million=row.cached_input_per_million,
        ),
        effective_at=row.effective_at,
        created_by=row.created_by,
        created_at=row.created_at,
    )
