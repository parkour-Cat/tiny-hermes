"""What an operator does to a sandbox host: drain, activate, retire (tier C §4.8).

In the Run module rather than the sandbox one because retiring is a Run
decision as much as a sandbox one: each sandbox released is a Run that may
now move on, and the one waiting on that destruction is moved here exactly
as the Scheduler's cleanup moves it.

Nothing here happens on its own. A host not heard from is only skipped by
placement: it may be cut off with its sandboxes still running, and releasing
those is a Run with two. `retire` is the operator saying the host is gone.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.runs.application.scheduler import confirm_cleanup_intent
from tiny_hermes.sandbox.domain.models import LIVE_RESERVATIONS, HostState, InstanceStatus
from tiny_hermes.sandbox.infrastructure.sql_store import SqlSandboxStore
from tiny_hermes.sandbox.infrastructure.tables import (
    SandboxHostRow,
    SandboxInstanceRow,
    SandboxReservationRow,
)

_LIVE = [e.value for e in LIVE_RESERVATIONS]


class UnknownSandboxHost(Exception):
    """No host by that name: named rather than quietly done, because a typo
    that retired nothing would leave the real host's Runs waiting while the
    operator believes they were freed."""


@dataclass(frozen=True)
class HostListing:
    name: str
    url: str | None
    state: HostState
    seen_at: datetime
    memory_budget_mb: int | None
    reserved_mb: int
    live_sandboxes: int


async def list_hosts(session: AsyncSession) -> list[HostListing]:
    reserved = (
        select(
            SandboxInstanceRow.controller.label("host"),
            func.sum(SandboxInstanceRow.memory_mb).label("mb"),
            func.count().label("live"),
        )
        .join(
            SandboxReservationRow,
            SandboxReservationRow.sandbox_instance_id == SandboxInstanceRow.id,
        )
        .where(SandboxReservationRow.status.in_(_LIVE))
        .group_by(SandboxInstanceRow.controller)
        .subquery()
    )
    rows = await session.execute(
        select(
            SandboxHostRow,
            func.coalesce(reserved.c.mb, 0),
            func.coalesce(reserved.c.live, 0),
        )
        .outerjoin(reserved, reserved.c.host == SandboxHostRow.name)
        .order_by(SandboxHostRow.name)
    )
    return [
        HostListing(
            name=host.name,
            url=host.url,
            state=HostState(host.state),
            seen_at=host.seen_at,
            memory_budget_mb=host.memory_budget_mb,
            reserved_mb=int(mb),
            live_sandboxes=int(live),
        )
        for host, mb, live in rows.all()
    ]


async def drain_host(session: AsyncSession, name: str) -> int:
    """No new sandboxes on this host. Returns how many live ones it still has:
    stop it for maintenance when that reaches zero."""
    await _set_state(session, name, HostState.DRAINING)
    return len(await _live_on(session, name))


async def activate_host(session: AsyncSession, name: str) -> None:
    await _set_state(session, name, HostState.ACTIVE)


async def retire_host(session: AsyncSession, name: str) -> int:
    """The host is gone: release every sandbox recorded on it. Returns how many.

    In one transaction with the state change, so placement never sees the
    host retired with claims still on it, nor the claims released with the
    host still taking new ones. Each released Run's next acquire lands on
    another host and restores cold from the object store. Retiring twice
    releases nothing the second time.
    """
    await _set_state(session, name, HostState.RETIRED)
    store = SqlSandboxStore(session)
    live = await _live_on(session, name)
    for reservation_id, run_id, instance_id in live:
        # Before the release: a stale address row would make whatever
        # container next holds that address this Run, to the egress proxy.
        await store.clear_egress_address(instance_id)
        await store.set_instance_status(instance_id, InstanceStatus.DESTROYED)
        await store.release(reservation_id)
        await confirm_cleanup_intent(session, run_id, instance_id)
    return len(live)


async def _set_state(session: AsyncSession, name: str, state: HostState) -> None:
    changed = await session.execute(
        update(SandboxHostRow)
        .where(SandboxHostRow.name == name)
        .values(state=state.value)
        .returning(SandboxHostRow.name)
    )
    if changed.first() is None:
        raise UnknownSandboxHost(name)


async def _live_on(session: AsyncSession, name: str) -> list[tuple[UUID, UUID, UUID]]:
    rows = await session.execute(
        select(
            SandboxReservationRow.id,
            SandboxReservationRow.run_id,
            SandboxReservationRow.sandbox_instance_id,
        )
        .join(
            SandboxInstanceRow,
            SandboxInstanceRow.id == SandboxReservationRow.sandbox_instance_id,
        )
        .where(SandboxInstanceRow.controller == name, SandboxReservationRow.status.in_(_LIVE))
        .with_for_update(of=SandboxReservationRow)
    )
    return [(row[0], row[1], row[2]) for row in rows.all()]
