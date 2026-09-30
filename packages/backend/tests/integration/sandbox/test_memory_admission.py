"""Sandbox memory admission (§21.3, v2.14), against a real daemon and database.

On 2026-09-30 nothing bounded the sum of sandbox memory: 24 sandboxes holding
9.6 GiB exhausted an 8 GiB host in 13 seconds, the OOM killer took `dockerd`,
and every service stopped (docs/superpowers/verification/2026-09-30-sandbox-oom.md).

Each call runs in its own transaction and commits, as the controller's socket
dispatch does: the admission lock and the eviction row locks only mean
anything between transactions.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from docker.errors import NotFound
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tiny_hermes.sandbox.application.controller import (
    AcquireResult,
    RefusalReason,
    SandboxController,
    SandboxRefused,
)
from tiny_hermes.sandbox.domain.container_policy import DEFAULT_PROFILE
from tiny_hermes.sandbox.domain.models import CacheState, ReservationStatus
from tiny_hermes.sandbox.infrastructure.docker_engine import DockerEngine
from tiny_hermes.sandbox.infrastructure.sql_store import SqlSandboxStore

from .conftest import LABEL, RecordingAudit, StubLeases

WORKSPACE = uuid4()
MiB = 1024 * 1024


class Platform:
    """One controller call per transaction, with a budget and a limit."""

    def __init__(
        self,
        engine: AsyncEngine,
        docker_client: Any,
        image_digest: str,
        leases: StubLeases,
        audit: RecordingAudit,
    ) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._docker = docker_client
        self._digest = image_digest
        self._leases = leases
        self.audit = audit
        self.leases: dict[UUID, UUID] = {}

    async def call[T](
        self,
        work: Callable[[SandboxController], Awaitable[T]],
        *,
        budget: int = 512,
        memory: int = 256,
    ) -> T:
        async with self._sessions() as session:
            controller = self._controller(session, budget=budget, memory=memory)
            try:
                answer = await work(controller)
            except SandboxRefused:
                await session.commit()
                raise
            except BaseException:
                await session.rollback()
                raise
            await session.commit()
            return answer

    def _controller(self, session: AsyncSession, *, budget: int, memory: int) -> SandboxController:
        return SandboxController(
            engine=DockerEngine(self._docker, extra_labels={LABEL: "1"}),
            store=SqlSandboxStore(session),
            approved_digests=(self._digest,),
            leases=self._leases,
            audit=self.audit,
            ceiling=replace(DEFAULT_PROFILE, memory_mb=memory),
            memory_budget_mb=budget,
        )

    async def acquire(self, run_id: UUID, **limits: int) -> AcquireResult:
        lease = self.leases.setdefault(run_id, uuid4())
        return await self.call(
            lambda c: c.acquire(
                run_id=run_id, lease_id=lease, workspace_id=WORKSPACE, profile="default"
            ),
            **limits,
        )

    async def park(self, run_id: UUID, sandbox_id: UUID) -> None:
        """What a slice boundary does: freeze, then keep warm."""
        lease = self.leases[run_id]
        await self.call(lambda c: c.freeze(run_id=run_id, lease_id=lease, sandbox_id=sandbox_id))
        until = datetime.now(UTC) + timedelta(minutes=5)
        await self.call(lambda c: c.keep(run_id=run_id, sandbox_id=sandbox_id, until=until))

    async def container_of(self, sandbox_id: UUID) -> str:
        async with self._sessions() as session:
            found = await SqlSandboxStore(session).read_instance(sandbox_id)
        assert found is not None
        return found.container_id

    async def claim_of(self, run_id: UUID) -> ReservationStatus | None:
        async with self._sessions() as session:
            found = await SqlSandboxStore(session).live_for_run(run_id)
        return None if found is None else found.status

    def labelled(self) -> int:
        return len(self._docker.containers.list(all=True, filters={"label": f"{LABEL}=1"}))

    def state(self, container_id: str) -> str | None:
        try:
            return str(self._docker.containers.get(container_id).status)
        except NotFound:
            return None


@pytest.fixture
def platform(
    engine: AsyncEngine,
    empty_database: None,
    docker_client: Any,
    image_digest: str,
    leases: StubLeases,
    audit: RecordingAudit,
) -> Platform:
    return Platform(engine, docker_client, image_digest, leases, audit)


async def test_a_sandbox_that_fits_is_created_with_the_limit_it_was_counted_at(
    platform: Platform, docker_client: Any
) -> None:
    made = await platform.acquire(uuid4())

    container = docker_client.containers.get(await platform.container_of(made.sandbox_id))
    assert container.attrs["HostConfig"]["Memory"] == 256 * MiB


async def test_a_sandbox_past_the_budget_is_refused_and_leaves_nothing_behind(
    platform: Platform,
) -> None:
    await platform.acquire(uuid4())
    await platform.acquire(uuid4())
    before = platform.labelled()
    third = uuid4()

    with pytest.raises(SandboxRefused) as refusal:
        await platform.acquire(third)

    assert refusal.value.reason is RefusalReason.MEMORY_BUDGET_EXHAUSTED
    assert platform.labelled() == before
    assert await platform.claim_of(third) is None


async def test_a_frozen_sandbox_of_another_run_is_evicted_to_make_room(
    platform: Platform,
) -> None:
    parked_run = uuid4()
    parked = await platform.acquire(parked_run)
    parked_container = await platform.container_of(parked.sandbox_id)
    await platform.park(parked_run, parked.sandbox_id)
    await platform.acquire(uuid4())

    await platform.acquire(uuid4())

    assert platform.state(parked_container) is None
    assert await platform.claim_of(parked_run) is None
    evictions = [entry for entry in platform.audit.entries if entry.action == "sandbox.evict"]
    assert [entry.run_id for entry in evictions] == [parked_run]


async def test_a_thawed_sandbox_is_never_evicted_though_its_claim_is_still_kept(
    platform: Platform,
) -> None:
    thawed_run = uuid4()
    thawed = await platform.acquire(thawed_run)
    await platform.park(thawed_run, thawed.sandbox_id)
    again = await platform.acquire(thawed_run)
    assert again.cache_state is CacheState.REUSED
    await platform.acquire(uuid4())

    with pytest.raises(SandboxRefused) as refusal:
        await platform.acquire(uuid4())

    assert refusal.value.reason is RefusalReason.MEMORY_BUDGET_EXHAUSTED
    assert platform.state(await platform.container_of(thawed.sandbox_id)) == "running"


async def test_thawing_its_own_frozen_sandbox_needs_no_room(platform: Platform) -> None:
    parked_run = uuid4()
    parked = await platform.acquire(parked_run)
    await platform.park(parked_run, parked.sandbox_id)
    await platform.acquire(uuid4())

    again = await platform.acquire(parked_run)

    assert again.cache_state is CacheState.REUSED
    assert again.sandbox_id == parked.sandbox_id


async def test_two_acquires_racing_for_the_last_room_do_not_both_get_it(
    platform: Platform,
) -> None:
    await platform.acquire(uuid4())

    outcomes = await asyncio.gather(
        platform.acquire(uuid4()), platform.acquire(uuid4()), return_exceptions=True
    )

    refused = [o for o in outcomes if isinstance(o, SandboxRefused)]
    made = [o for o in outcomes if isinstance(o, AcquireResult)]
    assert len(made) == 1
    assert [o.reason for o in refused] == [RefusalReason.MEMORY_BUDGET_EXHAUSTED]


async def test_a_warm_sandbox_is_not_destroyed_when_evicting_it_would_not_make_room(
    platform: Platform,
) -> None:
    """The operator raised the limit: one 256 MiB sandbox out of the way
    still leaves no room for a 512 MiB one."""
    parked_run = uuid4()
    parked = await platform.acquire(parked_run)
    await platform.park(parked_run, parked.sandbox_id)
    await platform.acquire(uuid4())

    with pytest.raises(SandboxRefused):
        await platform.acquire(uuid4(), memory=512)

    assert await platform.claim_of(parked_run) is ReservationStatus.KEPT
    assert platform.state(await platform.container_of(parked.sandbox_id)) == "paused"
