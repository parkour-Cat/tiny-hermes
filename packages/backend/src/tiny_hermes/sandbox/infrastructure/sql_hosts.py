"""The sandbox host registry (tier C §4.1, §4.6)."""

import random
from datetime import UTC, datetime, timedelta
from ipaddress import ip_network
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.sandbox.domain.models import (
    LIVE_RESERVATIONS,
    HostState,
    NetworkPoolOverlap,
    SandboxHost,
)
from tiny_hermes.sandbox.infrastructure.tables import (
    SandboxHostRow,
    SandboxInstanceRow,
    SandboxReservationRow,
)

#: Taken by registration and nothing else: two hosts each reading the other
#: absent and both writing overlapping pools is the race it closes.
_REGISTRATION_LOCK_KEY = 591_046_228

#: The states whose addresses may still be held. A retired host's sandboxes
#: were released with its addresses, so its pool is free to reuse.
_IN_USE = (HostState.ACTIVE.value, HostState.DRAINING.value)


def _host(row: SandboxHostRow) -> SandboxHost:
    return SandboxHost(
        name=row.name,
        url=row.url,
        memory_budget_mb=row.memory_budget_mb,
        network_pool=row.network_pool,
        state=HostState(row.state),
        seen_at=row.seen_at,
    )


class SqlSandboxHosts:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register(
        self,
        *,
        name: str,
        url: str | None,
        memory_budget_mb: int | None,
        network_pool: str | None,
    ) -> SandboxHost:
        """Write what this host says about itself, keeping its state.

        A new host starts `active`. A known one refreshes its address, budget,
        pool and heartbeat; its state is an operator's, so a drained or
        retired host stays that way however often it restarts.
        """
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _REGISTRATION_LOCK_KEY}
        )
        if network_pool is not None:
            await self._refuse_overlap(name, network_pool)
        now = datetime.now(UTC)
        told = {
            "url": url,
            "memory_budget_mb": memory_budget_mb,
            "network_pool": network_pool,
            "seen_at": now,
        }
        await self._session.execute(
            insert(SandboxHostRow)
            .values(name=name, state=HostState.ACTIVE.value, created_at=now, **told)
            .on_conflict_do_update(index_elements=[SandboxHostRow.name], set_=told)
        )
        found = await self.read(name)
        if found is None:  # written just above, in this transaction
            raise RuntimeError(f"sandbox host {name} vanished while registering")
        return found

    async def _refuse_overlap(self, name: str, pool: str) -> None:
        mine = ip_network(pool)
        others = await self._session.execute(
            select(SandboxHostRow.name, SandboxHostRow.network_pool).where(
                SandboxHostRow.name != name,
                SandboxHostRow.network_pool.is_not(None),
                SandboxHostRow.state.in_(_IN_USE),
            )
        )
        for other, other_pool in others.all():
            if other_pool is not None and mine.overlaps(ip_network(other_pool)):
                raise NetworkPoolOverlap(other=other, other_pool=other_pool)

    async def heartbeat(self, name: str) -> None:
        await self._session.execute(
            update(SandboxHostRow)
            .where(SandboxHostRow.name == name)
            .values(seen_at=datetime.now(UTC))
        )

    async def read(self, name: str) -> SandboxHost | None:
        row = await self._session.get(SandboxHostRow, name, populate_existing=True)
        return None if row is None else _host(row)

    async def set_state(self, name: str, state: HostState) -> None:
        await self._session.execute(
            update(SandboxHostRow).where(SandboxHostRow.name == name).values(state=state.value)
        )


class SqlHostDirectory:
    """What a pool reads to place and route (tier C §4.3, §4.4).

    One short session per read: a Worker's slice holds no transaction across
    a call to a host, and a stale answer here costs at most a refusal that
    the host's own admission gives.
    """

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], stale_seconds: int
    ) -> None:
        self._sessions = sessions
        self._stale = timedelta(seconds=stale_seconds)

    async def host_of_run(self, run_id: UUID) -> str | None:
        """The host of this Run's live claim, if it has one."""
        async with self._sessions() as session:
            return await session.scalar(
                select(SandboxInstanceRow.controller)
                .join(
                    SandboxReservationRow,
                    SandboxReservationRow.sandbox_instance_id == SandboxInstanceRow.id,
                )
                .where(
                    SandboxReservationRow.run_id == run_id,
                    SandboxReservationRow.status.in_([e.value for e in LIVE_RESERVATIONS]),
                )
                .limit(1)
            )

    async def host_of_sandbox(self, sandbox_id: UUID) -> str | None:
        async with self._sessions() as session:
            return await session.scalar(
                select(SandboxInstanceRow.controller).where(SandboxInstanceRow.id == sandbox_id)
            )

    async def url_of(self, name: str) -> str | None:
        async with self._sessions() as session:
            return await session.scalar(
                select(SandboxHostRow.url).where(SandboxHostRow.name == name)
            )

    async def placement(self) -> list[str]:
        """Hosts that may take a new sandbox, the most room first.

        Room is the budget less the limits of what live claims hold there,
        the same sum that host's admission makes. Equal room is shuffled, so
        Workers reading at once do not all try the same host first. Only
        `active` hosts heard from lately and with an address to dial.
        """
        reserved = (
            select(
                SandboxInstanceRow.controller.label("host"),
                func.sum(SandboxInstanceRow.memory_mb).label("mb"),
            )
            .join(
                SandboxReservationRow,
                SandboxReservationRow.sandbox_instance_id == SandboxInstanceRow.id,
            )
            .where(SandboxReservationRow.status.in_([e.value for e in LIVE_RESERVATIONS]))
            .group_by(SandboxInstanceRow.controller)
            .subquery()
        )
        async with self._sessions() as session:
            rows = await session.execute(
                select(
                    SandboxHostRow.name,
                    SandboxHostRow.memory_budget_mb,
                    func.coalesce(reserved.c.mb, 0),
                )
                .outerjoin(reserved, reserved.c.host == SandboxHostRow.name)
                .where(
                    SandboxHostRow.state == HostState.ACTIVE.value,
                    SandboxHostRow.url.is_not(None),
                    SandboxHostRow.seen_at >= datetime.now(UTC) - self._stale,
                )
            )
        room = [
            # A host that did not say its budget sorts last, not first.
            (name, -1 if budget is None else int(budget) - int(used), random.random())  # noqa: S311
            for name, budget, used in rows.all()
        ]
        return [name for name, _, _ in sorted(room, key=lambda r: (-r[1], r[2]))]
