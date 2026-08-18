from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.sandbox.domain.models import (
    LIVE_RESERVATIONS,
    InstanceStatus,
    ReservationStatus,
    SandboxInstance,
    SandboxReservation,
)
from tiny_hermes.sandbox.infrastructure.tables import (
    SandboxEgressAddressRow,
    SandboxInstanceRow,
    SandboxReservationRow,
)


class UnknownReservation(Exception):
    pass


class UnknownInstance(Exception):
    pass


def _reservation(row: SandboxReservationRow) -> SandboxReservation:
    return SandboxReservation(
        id=row.id,
        run_id=row.run_id,
        workspace_id=row.workspace_id,
        instance_id=row.sandbox_instance_id,
        status=ReservationStatus(row.status),
        idle_expires_at=row.idle_expires_at,
        isolation_reason=row.isolation_reason,
    )


def _instance(row: SandboxInstanceRow) -> SandboxInstance:
    return SandboxInstance(
        id=row.id,
        container_id=row.container_id,
        image_digest=row.image_digest,
        resource_profile=row.resource_profile,
        boot_id=row.boot_id,
        status=InstanceStatus(row.status),
    )


class SqlSandboxStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def reserve(
        self, *, run_id: UUID, workspace_id: UUID, instance: SandboxInstance
    ) -> SandboxReservation:
        self._session.add(
            SandboxInstanceRow(
                id=instance.id,
                container_id=instance.container_id,
                image_digest=instance.image_digest,
                resource_profile=instance.resource_profile,
                boot_id=instance.boot_id,
                status=instance.status.value,
            )
        )
        row = SandboxReservationRow(
            run_id=run_id,
            workspace_id=workspace_id,
            sandbox_instance_id=instance.id,
            status=ReservationStatus.ACTIVE.value,
        )
        self._session.add(row)
        # Forced here rather than at commit, so the caller sees the uniqueness
        # refusal at the point it asked rather than at the end of a transaction
        # that has since done other work.
        await self._session.flush()
        return _reservation(row)

    async def live_for_run(self, run_id: UUID) -> SandboxReservation | None:
        found = await self._session.execute(
            select(SandboxReservationRow).where(
                SandboxReservationRow.run_id == run_id,
                SandboxReservationRow.status.in_([e.value for e in LIVE_RESERVATIONS]),
            )
        )
        row = found.scalar_one_or_none()
        return None if row is None else _reservation(row)

    async def read(self, reservation_id: UUID) -> SandboxReservation | None:
        row = await self._session.get(SandboxReservationRow, reservation_id)
        return None if row is None else _reservation(row)

    async def keep(self, reservation_id: UUID, *, idle_expires_at: datetime) -> SandboxReservation:
        row = await self._require(reservation_id)
        row.status = ReservationStatus.KEPT.value
        row.idle_expires_at = idle_expires_at
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _reservation(row)

    async def isolate(self, reservation_id: UUID, *, reason: str) -> SandboxReservation:
        found = await self._session.execute(
            select(SandboxReservationRow)
            .where(SandboxReservationRow.id == reservation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        row = found.scalar_one_or_none()
        if row is None:
            raise UnknownReservation
        if ReservationStatus(row.status) not in LIVE_RESERVATIONS:
            return _reservation(row)
        row.status = ReservationStatus.ISOLATED.value
        row.isolation_reason = reason
        # A deadline on an isolated claim would put it in the Scheduler's expiry
        # scan, which destroys; an isolated container needs confirming, not
        # destroying on a timer.
        row.idle_expires_at = None
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _reservation(row)

    async def release(self, reservation_id: UUID) -> SandboxReservation:
        row = await self._require(reservation_id)
        row.status = ReservationStatus.RELEASED.value
        row.idle_expires_at = None
        row.isolation_reason = None
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _reservation(row)

    async def expired_keeps(self, now: datetime) -> list[SandboxReservation]:
        found = await self._session.execute(
            select(SandboxReservationRow)
            .where(
                SandboxReservationRow.status == ReservationStatus.KEPT.value,
                SandboxReservationRow.idle_expires_at <= now,
            )
            .order_by(SandboxReservationRow.idle_expires_at)
        )
        return [_reservation(row) for row in found.scalars()]

    async def isolated(self, limit: int) -> list[SandboxReservation]:
        """Reservations whose cleanup could not be confirmed.

        Retried rather than forgotten: a container the platform is unsure about
        is the one worth coming back to.
        """
        found = await self._session.execute(
            select(SandboxReservationRow)
            .where(SandboxReservationRow.status == ReservationStatus.ISOLATED.value)
            .order_by(SandboxReservationRow.updated_at)
            .limit(limit)
        )
        return [_reservation(row) for row in found.scalars()]

    async def read_instance(self, instance_id: UUID) -> SandboxInstance | None:
        row = await self._session.get(SandboxInstanceRow, instance_id)
        return None if row is None else _instance(row)

    async def register_egress_address(
        self, *, address: str, run_id: UUID, sandbox_id: UUID
    ) -> None:
        """Claim this address for this Run, taking it from whoever had it.

        A `merge` rather than an insert: Docker reuses addresses, and the
        container that has one now is the one that owns it. The row a previous
        container left behind is exactly the row that must not survive.
        """
        await self._session.merge(
            SandboxEgressAddressRow(
                address=address, run_id=run_id, sandbox_id=sandbox_id
            )
        )
        await self._session.flush()

    async def clear_egress_address(self, sandbox_id: UUID) -> None:
        rows = (
            await self._session.scalars(
                select(SandboxEgressAddressRow).where(
                    SandboxEgressAddressRow.sandbox_id == sandbox_id
                )
            )
        ).all()
        for row in rows:
            await self._session.delete(row)
        await self._session.flush()

    async def set_instance_status(
        self, instance_id: UUID, status: InstanceStatus
    ) -> SandboxInstance:
        row = await self._session.get(SandboxInstanceRow, instance_id)
        if row is None:
            raise UnknownInstance
        row.status = status.value
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _instance(row)

    async def _require(self, reservation_id: UUID) -> SandboxReservationRow:
        row = await self._session.get(SandboxReservationRow, reservation_id)
        if row is None:
            raise UnknownReservation
        return row
