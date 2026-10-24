from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, text
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
        memory_mb=row.memory_mb,
        controller=row.controller,
    )


#: Taken by sandbox admission and nothing else.
_ADMISSION_LOCK_KEY = 591_046_227

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
                memory_mb=instance.memory_mb,
                controller=instance.controller,
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

    async def live_for_run_locked(self, run_id: UUID) -> SandboxReservation | None:
        """`live_for_run`, holding the row until this transaction ends.

        Taken before thawing, so eviction — which skips locked rows — cannot
        remove the container between this read and the unpause.
        """
        found = await self._session.execute(
            select(SandboxReservationRow)
            .where(
                SandboxReservationRow.run_id == run_id,
                SandboxReservationRow.status.in_([e.value for e in LIVE_RESERVATIONS]),
            )
            .with_for_update()
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

    async def lock_admission(self, controller: str = "default") -> None:
        """Held until this transaction ends, by every acquire that adds memory.

        Counting, creating and inserting under it is what stops two acquires
        from both counting the same free space. One lock per sandbox host
        (tier C): each host's budget is its own memory, so two hosts' acquires
        have nothing to wait on each other for.
        """
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:key, hashtext(:controller))"),
            {"key": _ADMISSION_LOCK_KEY, "controller": controller},
        )

    async def committed_memory_mb(self, controller: str | None = None) -> int:
        """The limits of every container a live claim may still hold.

        Isolated claims count: nobody could confirm their container went.
        One host's, or with no host named the whole pool's (the gauge).
        """
        query = (
            select(func.coalesce(func.sum(SandboxInstanceRow.memory_mb), 0))
            .join(
                SandboxReservationRow,
                SandboxReservationRow.sandbox_instance_id == SandboxInstanceRow.id,
            )
            .where(SandboxReservationRow.status.in_([e.value for e in LIVE_RESERVATIONS]))
        )
        if controller is not None:
            query = query.where(SandboxInstanceRow.controller == controller)
        total = await self._session.scalar(query)
        return int(total or 0)

    async def live_instances_by_status(self) -> dict[str, int]:
        """Instances a live claim may still hold, by their own status."""
        rows = await self._session.execute(
            select(SandboxInstanceRow.status, func.count())
            .join(
                SandboxReservationRow,
                SandboxReservationRow.sandbox_instance_id == SandboxInstanceRow.id,
            )
            .where(SandboxReservationRow.status.in_([e.value for e in LIVE_RESERVATIONS]))
            .group_by(SandboxInstanceRow.status)
        )
        return {str(status): int(count) for status, count in rows.all()}

    async def evictable_keeps(self, controller: str | None = None) -> list[SandboxReservation]:
        """Frozen instances under a kept claim, the earliest deadline first.

        Candidates, not verdicts: a thawed instance keeps its `kept` claim
        while it runs, and every checkpoint freezes it, so the caller must also
        see that the Run holds no live lease. Locked, skipping any row another
        transaction holds: the Run that owns it may be thawing it right now.
        """
        query = (
            select(SandboxReservationRow)
            .join(
                SandboxInstanceRow,
                SandboxInstanceRow.id == SandboxReservationRow.sandbox_instance_id,
            )
            .where(
                SandboxReservationRow.status == ReservationStatus.KEPT.value,
                SandboxInstanceRow.status == InstanceStatus.FROZEN.value,
            )
        )
        if controller is not None:
            # Evicting on another host frees nothing on this one (tier C).
            query = query.where(SandboxInstanceRow.controller == controller)
        found = await self._session.execute(
            query.order_by(
                SandboxReservationRow.idle_expires_at, SandboxReservationRow.id
            ).with_for_update(of=SandboxReservationRow, skip_locked=True)
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

    async def complete_instance(self, instance_id: UUID, container_id: str) -> SandboxInstance:
        """A `creating` instance's container is built: record it, now running."""
        row = await self._session.get(SandboxInstanceRow, instance_id)
        if row is None:
            raise UnknownInstance
        row.container_id = container_id
        row.status = InstanceStatus.RUNNING.value
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _instance(row)

    async def _require(self, reservation_id: UUID) -> SandboxReservationRow:
        row = await self._session.get(SandboxReservationRow, reservation_id)
        if row is None:
            raise UnknownReservation
        return row
