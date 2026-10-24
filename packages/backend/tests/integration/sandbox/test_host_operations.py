"""drain, activate and retire: what an operator does to a sandbox host (tier C §4.8)."""

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tiny_hermes.runs.application.sandbox_hosts import (
    UnknownSandboxHost,
    activate_host,
    drain_host,
    list_hosts,
    retire_host,
)
from tiny_hermes.sandbox.domain.models import (
    HostState,
    InstanceStatus,
    ReservationStatus,
    SandboxInstance,
)
from tiny_hermes.sandbox.infrastructure.sql_hosts import SqlSandboxHosts
from tiny_hermes.sandbox.infrastructure.sql_store import SqlSandboxStore

pytestmark = pytest.mark.usefixtures("empty_database")


async def host(engine: AsyncEngine, name: str, budget: int = 4096) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        await SqlSandboxHosts(session).register(
            name=name, url=f"tls://{name}:7700", memory_budget_mb=budget, network_pool=None
        )
        await session.commit()


async def place(engine: AsyncEngine, name: str, memory_mb: int = 1024) -> tuple[UUID, UUID]:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    made = SandboxInstance(
        id=uuid4(),
        container_id=uuid4().hex,
        image_digest="sha256:" + "a" * 64,
        resource_profile="default",
        boot_id=uuid4().hex,
        status=InstanceStatus.FROZEN,
        memory_mb=memory_mb,
        controller=name,
    )
    run_id = uuid4()
    async with sessions() as session:
        await SqlSandboxStore(session).reserve(
            run_id=run_id, workspace_id=uuid4(), instance=made
        )
        await session.commit()
    return run_id, made.id


async def claim_of(engine: AsyncEngine, run_id: UUID) -> ReservationStatus | None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        found = await SqlSandboxStore(session).live_for_run(run_id)
    return None if found is None else found.status


async def state_of(engine: AsyncEngine, name: str) -> HostState | None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        found = await SqlSandboxHosts(session).read(name)
    return None if found is None else found.state


async def run(
    engine: AsyncEngine, operation: Callable[[AsyncSession, str], Awaitable[Any]], name: str
) -> Any:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        answer = await operation(session, name)
        await session.commit()
    return answer


async def test_retiring_a_host_releases_its_sandboxes_and_only_its(engine: AsyncEngine) -> None:
    await host(engine, "sbx-a")
    await host(engine, "sbx-b")
    on_a, sandbox_a = await place(engine, "sbx-a")
    on_b, _ = await place(engine, "sbx-b")

    released = await run(engine, retire_host, "sbx-a")

    assert released == 1
    assert await state_of(engine, "sbx-a") is HostState.RETIRED
    assert await claim_of(engine, on_a) is None, "released: the Run may reserve again"
    assert await claim_of(engine, on_b) is ReservationStatus.ACTIVE
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        gone = await SqlSandboxStore(session).read_instance(sandbox_a)
    assert gone is not None and gone.status is InstanceStatus.DESTROYED


async def test_retiring_twice_releases_nothing_the_second_time(engine: AsyncEngine) -> None:
    await host(engine, "sbx-a")
    await place(engine, "sbx-a")
    await run(engine, retire_host, "sbx-a")

    assert await run(engine, retire_host, "sbx-a") == 0


async def test_an_unknown_host_is_named_rather_than_quietly_done(engine: AsyncEngine) -> None:
    """A typo retiring nothing and saying "done" would leave the real host's
    Runs waiting while the operator believes they were freed."""
    with pytest.raises(UnknownSandboxHost):
        await run(engine, retire_host, "sbx-typo")


async def test_draining_says_how_many_sandboxes_are_left(engine: AsyncEngine) -> None:
    """The operator stops the host when this reaches zero."""
    await host(engine, "sbx-a")
    await place(engine, "sbx-a")
    await place(engine, "sbx-a")

    left = await run(engine, drain_host, "sbx-a")

    assert left == 2
    assert await state_of(engine, "sbx-a") is HostState.DRAINING


async def test_activating_puts_a_host_back_into_placement(engine: AsyncEngine) -> None:
    await host(engine, "sbx-a")
    await run(engine, drain_host, "sbx-a")

    await run(engine, activate_host, "sbx-a")

    assert await state_of(engine, "sbx-a") is HostState.ACTIVE


async def test_the_listing_shows_each_hosts_state_and_room(engine: AsyncEngine) -> None:
    await host(engine, "sbx-a", budget=4096)
    await host(engine, "sbx-b", budget=2048)
    await place(engine, "sbx-a", 1024)
    await run(engine, drain_host, "sbx-b")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async with sessions() as session:
        listed = {row.name: row for row in await list_hosts(session)}

    assert listed["sbx-a"].state is HostState.ACTIVE
    assert listed["sbx-a"].reserved_mb == 1024
    assert listed["sbx-a"].live_sandboxes == 1
    assert listed["sbx-b"].state is HostState.DRAINING
    assert listed["sbx-b"].reserved_mb == 0
