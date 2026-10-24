"""The sandbox host registry (tier C §4.1, §4.6)."""

from datetime import UTC, datetime
from ipaddress import ip_network

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.sandbox.domain.models import HostState, NetworkPoolOverlap, SandboxHost
from tiny_hermes.sandbox.infrastructure.tables import SandboxHostRow

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
