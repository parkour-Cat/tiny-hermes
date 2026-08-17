from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.model_catalog.domain.models import (
    ContextAccounting,
    EndpointStatus,
    ModelEndpoint,
    ModelEndpointSpec,
    UsageQuality,
)
from tiny_hermes.model_catalog.infrastructure.tables import ModelEndpointRow
from tiny_hermes.model_catalog.ports.store import EndpointNameTaken


def _to_domain(row: ModelEndpointRow) -> ModelEndpoint:
    return ModelEndpoint(
        id=row.id,
        spec=ModelEndpointSpec(
            name=row.name,
            kind="openai_compatible",
            base_url=row.base_url,
            model=row.model,
            context_window=row.context_window,
            max_output_tokens=row.max_output_tokens,
            usage_quality=UsageQuality(row.usage_quality),
            credential_ref=row.credential_ref,
            context_accounting=ContextAccounting(row.context_accounting),
            tokenizer=row.tokenizer,
        ),
        status=EndpointStatus(row.status),
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlModelEndpointStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register(self, spec: ModelEndpointSpec, created_by: UUID) -> ModelEndpoint:
        now = datetime.now(UTC)
        row = ModelEndpointRow(
            id=uuid4(),
            name=spec.name,
            kind=spec.kind,
            base_url=spec.base_url,
            model=spec.model,
            context_window=spec.context_window,
            max_output_tokens=spec.max_output_tokens,
            usage_quality=spec.usage_quality.value,
            credential_ref=spec.credential_ref,
            context_accounting=spec.context_accounting.value,
            tokenizer=spec.tokenizer,
            status=EndpointStatus.ACTIVE.value,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        try:
            await self._session.commit()
        except IntegrityError as clash:
            # The unique index is the authority. Reading first and inserting
            # after would let two administrators through the same gap.
            await self._session.rollback()
            raise EndpointNameTaken(spec.name) from clash
        return _to_domain(row)

    async def read(self, endpoint_id: UUID) -> ModelEndpoint | None:
        row = await self._session.get(ModelEndpointRow, endpoint_id)
        return None if row is None else _to_domain(row)

    async def list_active(self) -> list[ModelEndpoint]:
        found = await self._session.execute(
            select(ModelEndpointRow)
            .where(ModelEndpointRow.status == EndpointStatus.ACTIVE.value)
            .order_by(ModelEndpointRow.name)
        )
        return [_to_domain(row) for row in found.scalars()]

    async def update(self, endpoint_id: UUID, spec: ModelEndpointSpec) -> ModelEndpoint | None:
        row = await self._session.get(ModelEndpointRow, endpoint_id)
        if row is None:
            return None
        row.name = spec.name
        row.base_url = spec.base_url
        row.model = spec.model
        row.context_window = spec.context_window
        row.max_output_tokens = spec.max_output_tokens
        row.usage_quality = spec.usage_quality.value
        row.credential_ref = spec.credential_ref
        row.context_accounting = spec.context_accounting.value
        row.tokenizer = spec.tokenizer
        row.updated_at = datetime.now(UTC)
        try:
            await self._session.commit()
        except IntegrityError as clash:
            await self._session.rollback()
            raise EndpointNameTaken(spec.name) from clash
        return _to_domain(row)

    async def set_status(
        self, endpoint_id: UUID, status: EndpointStatus
    ) -> ModelEndpoint | None:
        row = await self._session.get(ModelEndpointRow, endpoint_id)
        if row is None:
            return None
        row.status = status.value
        row.updated_at = datetime.now(UTC)
        await self._session.commit()
        return _to_domain(row)
