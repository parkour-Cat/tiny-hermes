"""Sandbox hosts register themselves and keep a heartbeat (tier C step 3).

Design 2026-10-22 §4.1 and §4.6: each controller writes its own row when it
starts and every ten seconds after. A restart refreshes what it says about
itself and never its state, so a host an operator drained or retired does not
come back by restarting. Its network pool may not overlap another host's that
is still in use: addresses are unique only because pools are disjoint.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.sandbox.domain.models import (
    HostState,
    InstanceStatus,
    NetworkPoolOverlap,
    SandboxHost,
    SandboxInstance,
)
from tiny_hermes.sandbox.infrastructure.sql_hosts import SqlHostDirectory, SqlSandboxHosts
from tiny_hermes.sandbox.infrastructure.sql_store import SqlSandboxStore

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


# -- the directory placement and routing read (tier C §4.3, §4.4) --------------


async def place(
    engine: AsyncEngine, host: str, memory_mb: int, *, run_id: UUID | None = None
) -> UUID:
    """A live sandbox of `memory_mb` on `host`, as an acquire leaves it."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    made = SandboxInstance(
        id=uuid4(),
        container_id="c" * 64,
        image_digest="sha256:" + "a" * 64,
        resource_profile="default",
        boot_id=uuid4().hex,
        status=InstanceStatus.RUNNING,
        memory_mb=memory_mb,
        controller=host,
    )
    async with sessions() as session:
        await SqlSandboxStore(session).reserve(
            run_id=run_id or uuid4(), workspace_id=uuid4(), instance=made
        )
        await session.commit()
    return made.id


def directory(engine: AsyncEngine, stale_seconds: int = 30) -> SqlHostDirectory:
    return SqlHostDirectory(async_sessionmaker(engine, expire_on_commit=False), stale_seconds)


async def test_placement_puts_the_emptiest_host_first(engine: AsyncEngine) -> None:
    """Spread, not packed (product design §1.23): one host failing then
    touches the fewest Runs."""
    for name in ("sbx-a", "sbx-b", "sbx-c"):
        await register(engine, name, url=f"tls://{name}:7700", budget=4096)
    await place(engine, "sbx-a", 1024)
    await place(engine, "sbx-a", 1024)
    await place(engine, "sbx-c", 1024)

    assert await directory(engine).placement() == ["sbx-b", "sbx-c", "sbx-a"]


async def test_remaining_room_is_the_budget_less_what_is_reserved(engine: AsyncEngine) -> None:
    """A big host with more reserved can still have more room than a small
    empty one."""
    await register(engine, "big", url="tls://big:7700", budget=16384)
    await register(engine, "small", url="tls://small:7700", budget=2048)
    await place(engine, "big", 4096)

    assert await directory(engine).placement() == ["big", "small"]


@pytest.mark.parametrize("state", [HostState.DRAINING, HostState.RETIRED])
async def test_placement_skips_a_host_taking_no_new_sandboxes(
    engine: AsyncEngine, state: HostState
) -> None:
    await register(engine, "sbx-a", url="tls://sbx-a:7700")
    await register(engine, "sbx-b", url="tls://sbx-b:7700")
    await set_state(engine, "sbx-a", state)

    assert await directory(engine).placement() == ["sbx-b"]


async def test_placement_skips_a_host_not_heard_from(engine: AsyncEngine) -> None:
    """Skipped, and nothing more: a host that is only cut off may still be
    running its sandboxes (§4.8)."""
    await register(engine, "sbx-a", url="tls://sbx-a:7700")
    await register(engine, "sbx-b", url="tls://sbx-b:7700")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE sandbox_hosts SET seen_at = now() - interval '5 minutes' "
                "WHERE name = 'sbx-a'"
            )
        )

    assert await directory(engine).placement() == ["sbx-b"]


async def test_placement_skips_a_host_nobody_can_dial(engine: AsyncEngine) -> None:
    await register(engine, "default", url=None)
    await register(engine, "sbx-b", url="tls://sbx-b:7700")

    assert await directory(engine).placement() == ["sbx-b"]


async def test_a_run_is_routed_to_the_host_of_its_live_claim(engine: AsyncEngine) -> None:
    run = uuid4()
    sandbox = await place(engine, "sbx-b", 256, run_id=run)
    found = directory(engine)

    assert await found.host_of_run(run) == "sbx-b"
    assert await found.host_of_run(uuid4()) is None
    assert await found.host_of_sandbox(sandbox) == "sbx-b"
    assert await found.host_of_sandbox(uuid4()) is None


async def test_a_hosts_address_is_what_it_registered(engine: AsyncEngine) -> None:
    await register(engine, "sbx-a", url="tls://10.0.0.5:7700")
    found = directory(engine)

    assert await found.url_of("sbx-a") == "tls://10.0.0.5:7700"
    assert await found.url_of("nobody") is None
