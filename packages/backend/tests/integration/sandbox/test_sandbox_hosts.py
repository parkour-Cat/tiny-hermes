"""Sandbox hosts register themselves and keep a heartbeat (tier C step 3).

Design 2026-10-22 §4.1 and §4.6: each controller writes its own row when it
starts and every ten seconds after. A restart refreshes what it says about
itself and never its state, so a host an operator drained or retired does not
come back by restarting. Its network pool may not overlap another host's that
is still in use: addresses are unique only because pools are disjoint.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.sandbox.domain.models import HostState, NetworkPoolOverlap, SandboxHost
from tiny_hermes.sandbox.infrastructure.sql_hosts import SqlSandboxHosts

pytestmark = pytest.mark.usefixtures("empty_database")


async def register(
    engine: AsyncEngine,
    name: str,
    *,
    url: str | None = None,
    budget: int | None = 4096,
    pool: str | None = None,
) -> SandboxHost:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        host = await SqlSandboxHosts(session).register(
            name=name, url=url, memory_budget_mb=budget, network_pool=pool
        )
        await session.commit()
    return host


async def read(engine: AsyncEngine, name: str) -> SandboxHost | None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        return await SqlSandboxHosts(session).read(name)


async def set_state(engine: AsyncEngine, name: str, state: HostState) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        await SqlSandboxHosts(session).set_state(name, state)
        await session.commit()


async def test_a_host_registers_what_it_says_about_itself(engine: AsyncEngine) -> None:
    before = datetime.now(UTC) - timedelta(seconds=5)

    host = await register(
        engine, "sbx-a", url="tls://10.0.0.5:7700", budget=4002, pool="10.240.0.0/16"
    )

    assert host.name == "sbx-a"
    assert host.url == "tls://10.0.0.5:7700"
    assert host.memory_budget_mb == 4002
    assert host.network_pool == "10.240.0.0/16"
    assert host.state is HostState.ACTIVE
    assert host.seen_at >= before


async def test_restarting_refreshes_what_it_says_but_not_its_state(engine: AsyncEngine) -> None:
    await register(engine, "sbx-a", url="tls://10.0.0.5:7700", budget=4002)
    await set_state(engine, "sbx-a", HostState.DRAINING)

    again = await register(engine, "sbx-a", url="tls://10.0.0.9:7700", budget=6000)

    assert again.url == "tls://10.0.0.9:7700"
    assert again.memory_budget_mb == 6000
    assert again.state is HostState.DRAINING


async def test_a_retired_host_does_not_come_back_by_restarting(engine: AsyncEngine) -> None:
    """§4.8: a host whose sandboxes were released after it died must not take
    new ones because its controller came back up."""
    await register(engine, "sbx-a")
    await set_state(engine, "sbx-a", HostState.RETIRED)

    again = await register(engine, "sbx-a")

    assert again.state is HostState.RETIRED


@pytest.mark.parametrize("state", [HostState.ACTIVE, HostState.DRAINING])
async def test_a_pool_overlapping_a_host_in_use_is_refused_naming_it(
    engine: AsyncEngine, state: HostState
) -> None:
    await register(engine, "sbx-a", pool="10.240.0.0/16")
    await set_state(engine, "sbx-a", state)

    with pytest.raises(NetworkPoolOverlap) as overlap:
        await register(engine, "sbx-b", pool="10.240.16.0/20")

    assert overlap.value.other == "sbx-a"
    assert overlap.value.other_pool == "10.240.0.0/16"
    assert await read(engine, "sbx-b") is None


async def test_a_retired_hosts_pool_may_be_reused(engine: AsyncEngine) -> None:
    """Retiring released every address it held, so nothing can collide."""
    await register(engine, "sbx-a", pool="10.240.0.0/16")
    await set_state(engine, "sbx-a", HostState.RETIRED)

    await register(engine, "sbx-b", pool="10.240.0.0/16")


async def test_disjoint_pools_and_hosts_without_one_register(engine: AsyncEngine) -> None:
    await register(engine, "sbx-a", pool="10.240.0.0/16")
    await register(engine, "sbx-b", pool="10.241.0.0/16")
    await register(engine, "sbx-c", pool=None)

    assert {h.name for h in [await read(engine, n) for n in ("sbx-a", "sbx-b", "sbx-c")] if h} == {
        "sbx-a",
        "sbx-b",
        "sbx-c",
    }


async def test_a_host_does_not_overlap_itself(engine: AsyncEngine) -> None:
    await register(engine, "sbx-a", pool="10.240.0.0/16")

    await register(engine, "sbx-a", pool="10.240.0.0/16")


async def test_two_hosts_registering_overlapping_pools_at_once_do_not_both_get_in(
    engine: AsyncEngine,
) -> None:
    """Each reads the others before writing itself; without a lock both read
    an empty table and both write."""
    outcomes = await asyncio.gather(
        register(engine, "sbx-a", pool="10.240.0.0/16"),
        register(engine, "sbx-b", pool="10.240.0.0/16"),
        return_exceptions=True,
    )

    assert sum(isinstance(o, NetworkPoolOverlap) for o in outcomes) == 1


async def test_a_heartbeat_moves_seen_at_forward(engine: AsyncEngine) -> None:
    first = await register(engine, "sbx-a")
    await asyncio.sleep(0.05)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        await SqlSandboxHosts(session).heartbeat("sbx-a")
        await session.commit()

    later = await read(engine, "sbx-a")

    assert later is not None
    assert later.seen_at > first.seen_at
