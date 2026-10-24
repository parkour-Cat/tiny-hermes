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
from tiny_hermes.sandbox.domain.models import (
    CacheState,
    InstanceStatus,
    ReservationStatus,
    SandboxInstance,
)
from tiny_hermes.sandbox.infrastructure.docker_engine import DockerEngine
from tiny_hermes.sandbox.infrastructure.sql_store import SqlSandboxStore
from tiny_hermes.sandbox.observation import publish_sandbox_gauges

from ..support import metric
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
        #: Replaced by tests that need Docker slower, failing, or paused.
        self.engine_type: type[DockerEngine] = DockerEngine

    async def call[T](
        self,
        work: Callable[[SandboxController], Awaitable[T]],
        *,
        budget: int = 512,
        memory: int = 256,
        memory_ceiling: int | None = None,
        host: str = "default",
    ) -> T:
        async with self._sessions() as session:
            controller = self._controller(
                session, budget=budget, memory=memory, memory_ceiling=memory_ceiling, host=host
            )
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

    def _controller(
        self,
        session: AsyncSession,
        *,
        budget: int,
        memory: int,
        memory_ceiling: int | None,
        host: str,
    ) -> SandboxController:
        return SandboxController(
            engine=self.engine_type(self._docker, extra_labels={LABEL: "1"}),
            store=SqlSandboxStore(session),
            # As the socket dispatch wires it (v2.24): the acquire commits its
            # registration before it creates the container.
            commit=session.commit,
            approved_digests=(self._digest,),
            leases=self._leases,
            audit=self.audit,
            ceiling=replace(DEFAULT_PROFILE, memory_mb=memory),
            memory_budget_mb=budget,
            memory_ceiling_mb=memory_ceiling,
            name=host,
        )

    async def acquire(
        self, run_id: UUID, *, asked: int | None = None, **limits: Any
    ) -> AcquireResult:
        """What a Worker does holding a live lease for the Run.

        `asked` is the Agent's `sandbox.memory_mb` (§21.3, v2.22); `None` is
        an Agent that declared none, and gets the instance default.
        """
        lease = self.leases.setdefault(run_id, uuid4())
        self._leases.renew(run_id=run_id)
        return await self.call(
            lambda c: c.acquire(
                run_id=run_id,
                lease_id=lease,
                workspace_id=WORKSPACE,
                profile="default",
                memory_mb=asked,
            ),
            **limits,
        )

    async def host_of(self, sandbox_id: UUID) -> str:
        async with self._sessions() as session:
            found = await SqlSandboxStore(session).read_instance(sandbox_id)
        assert found is not None
        return found.controller

    async def instance_of(self, sandbox_id: UUID) -> SandboxInstance:
        async with self._sessions() as session:
            found = await SqlSandboxStore(session).read_instance(sandbox_id)
        assert found is not None
        return found

    async def committed(self, host: str = "default") -> int:
        async with self._sessions() as session:
            return await SqlSandboxStore(session).committed_memory_mb(host)

    async def memory_of(self, sandbox_id: UUID) -> int:
        async with self._sessions() as session:
            found = await SqlSandboxStore(session).read_instance(sandbox_id)
        assert found is not None
        return found.memory_mb

    async def park(self, run_id: UUID, sandbox_id: UUID, *, host: str = "default") -> None:
        """What a slice boundary does: freeze, keep warm, release the lease."""
        await self.checkpoint(run_id, sandbox_id, host=host)
        until = datetime.now(UTC) + timedelta(minutes=5)
        await self.call(
            lambda c: c.keep(run_id=run_id, sandbox_id=sandbox_id, until=until), host=host
        )
        self._leases.expire(run_id=run_id)

    async def checkpoint(self, run_id: UUID, sandbox_id: UUID, *, host: str = "default") -> None:
        """Every round's workspace commit freezes the container first."""
        lease = self.leases[run_id]
        await self.call(
            lambda c: c.freeze(run_id=run_id, lease_id=lease, sandbox_id=sandbox_id), host=host
        )

    async def container_of(self, sandbox_id: UUID) -> str:
        async with self._sessions() as session:
            found = await SqlSandboxStore(session).read_instance(sandbox_id)
        assert found is not None
        return found.container_id

    def expire_lease(self, run_id: UUID) -> None:
        """What happens to a Worker's lease when the process holding it dies."""
        self._leases.expire(run_id=run_id)

    async def claim_of_instance(self, run_id: UUID) -> UUID:
        async with self._sessions() as session:
            found = await SqlSandboxStore(session).live_for_run(run_id)
        assert found is not None
        return found.instance_id

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
    rejected = metric("tiny_hermes_sandbox_admission_rejected_total")

    with pytest.raises(SandboxRefused) as refusal:
        await platform.acquire(third)

    assert refusal.value.reason is RefusalReason.MEMORY_BUDGET_EXHAUSTED
    assert metric("tiny_hermes_sandbox_admission_rejected_total") == rejected + 1
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


async def test_the_created_container_may_not_swap(platform: Platform, docker_client: Any) -> None:
    """Read back from the daemon: a setting in the dict proves only that we
    can build a dict."""
    made = await platform.acquire(uuid4())

    host = docker_client.containers.get(await platform.container_of(made.sandbox_id)).attrs[
        "HostConfig"
    ]
    assert host["MemorySwap"] == host["Memory"] == 256 * MiB


async def test_a_running_runs_sandbox_frozen_for_a_checkpoint_is_not_evicted(
    platform: Platform,
) -> None:
    """Every round's checkpoint freezes the container, and a Run thawed at a
    slice boundary keeps its `kept` claim: mid-checkpoint, a running Run's
    sandbox is frozen under a kept claim, like a parked one. On 2026-09-30
    eviction took such a sandbox and its Run was interrupted mid-round."""
    running_run = uuid4()
    made = await platform.acquire(running_run)
    await platform.park(running_run, made.sandbox_id)
    assert (await platform.acquire(running_run)).cache_state is CacheState.REUSED
    await platform.checkpoint(running_run, made.sandbox_id)
    await platform.acquire(uuid4())

    with pytest.raises(SandboxRefused) as refusal:
        await platform.acquire(uuid4())

    assert refusal.value.reason is RefusalReason.MEMORY_BUDGET_EXHAUSTED
    assert platform.state(await platform.container_of(made.sandbox_id)) == "paused"


async def test_the_sandboxes_and_their_memory_are_published(
    platform: Platform, engine: AsyncEngine
) -> None:
    """Observability design §3.4: what the budget is spent on, from the rows."""
    first = await platform.acquire(uuid4())
    second_run = uuid4()
    second = await platform.acquire(second_run)
    await platform.checkpoint(second_run, second.sandbox_id)
    assert first.sandbox_id != second.sandbox_id

    async with async_sessionmaker(engine)() as session:
        await publish_sandbox_gauges(SqlSandboxStore(session), budget_mb=512)

    assert metric("tiny_hermes_sandboxes", state="running") == 1
    assert metric("tiny_hermes_sandboxes", state="frozen") == 1
    assert metric("tiny_hermes_sandbox_memory_reserved_bytes") == 512 * MiB
    assert metric("tiny_hermes_sandbox_memory_budget_bytes") == 512 * MiB


# -- an Agent's own size (§21.3, v2.22) ----------------------------------------


async def test_an_agents_declared_size_is_the_containers_limit_and_what_is_counted(
    platform: Platform, docker_client: Any
) -> None:
    """The size has to reach both ends: the limit Docker enforces and the
    number admission adds up. Reaching only one is either an Agent killed at
    a size it did not ask for, or a host admitting by the wrong sum."""
    made = await platform.acquire(uuid4(), asked=384, memory=512, memory_ceiling=1024)

    container = docker_client.containers.get(await platform.container_of(made.sandbox_id))
    assert container.attrs["HostConfig"]["Memory"] == 384 * MiB
    assert await platform.memory_of(made.sandbox_id) == 384


async def test_small_sandboxes_fit_where_default_ones_would_not(platform: Platform) -> None:
    """The probe's finding as a test: a budget of 512 holds one default
    sandbox of 512, but two of an Agent's declared 256."""
    await platform.acquire(uuid4(), asked=256, budget=512, memory=512, memory_ceiling=1024)
    await platform.acquire(uuid4(), asked=256, budget=512, memory=512, memory_ceiling=1024)

    with pytest.raises(SandboxRefused) as refusal:
        await platform.acquire(uuid4(), budget=512, memory=512, memory_ceiling=1024)

    assert refusal.value.reason is RefusalReason.MEMORY_BUDGET_EXHAUSTED


async def test_a_size_above_the_ceiling_is_refused_and_leaves_nothing_behind(
    platform: Platform,
) -> None:
    """Publishing checks the ceiling; this is what still stands when a version
    was published before the ceiling came down."""
    run = uuid4()
    with pytest.raises(SandboxRefused) as refusal:
        await platform.acquire(run, asked=2048, budget=4096, memory=512, memory_ceiling=1024)

    assert refusal.value.reason is RefusalReason.MEMORY_OUT_OF_RANGE
    assert platform.labelled() == 0
    assert await platform.claim_of(run) is None


async def test_a_size_below_the_floor_is_refused(platform: Platform) -> None:
    with pytest.raises(SandboxRefused) as refusal:
        await platform.acquire(uuid4(), asked=128, memory=512, memory_ceiling=1024)

    assert refusal.value.reason is RefusalReason.MEMORY_OUT_OF_RANGE


async def test_a_size_larger_than_the_whole_budget_fails_rather_than_waits(
    platform: Platform,
) -> None:
    """`memory_budget_exhausted` puts the Run back in the queue to try again
    in ten seconds. A sandbox bigger than the whole budget never fits, so
    that would be a Run queued forever, retrying every ten seconds."""
    with pytest.raises(SandboxRefused) as refusal:
        await platform.acquire(uuid4(), asked=1024, budget=768, memory=256, memory_ceiling=4096)

    assert refusal.value.reason is RefusalReason.MEMORY_OUT_OF_RANGE
    assert platform.labelled() == 0


async def test_without_a_ceiling_only_the_default_may_be_asked_for(platform: Platform) -> None:
    """A controller built before v2.22's setting is threaded through admits
    the default and nothing larger, as it always did."""
    with pytest.raises(SandboxRefused) as refusal:
        await platform.acquire(uuid4(), asked=512, memory=256)

    assert refusal.value.reason is RefusalReason.MEMORY_OUT_OF_RANGE


# -- one budget per sandbox host (tier C, §21.3 v2.23) -------------------------
#
# Two controllers with two names on one daemon and one database: what a pool
# of hosts shares (the database) without what it does not (each host's own
# Docker). Each host's budget is its own memory, so each must count only its
# own sandboxes, evict only its own, and touch nothing recorded elsewhere.


async def test_a_sandbox_records_the_host_that_made_it(platform: Platform) -> None:
    made = await platform.acquire(uuid4(), host="sbx-a")

    assert await platform.host_of(made.sandbox_id) == "sbx-a"


async def test_each_host_admits_against_its_own_sandboxes_only(platform: Platform) -> None:
    """A budget of 512 holds two 256 sandboxes. Counted across the pool, the
    third would be refused on a host that has nothing on it."""
    await platform.acquire(uuid4(), host="sbx-a")
    await platform.acquire(uuid4(), host="sbx-a")

    with pytest.raises(SandboxRefused) as full:
        await platform.acquire(uuid4(), host="sbx-a")
    assert full.value.reason is RefusalReason.MEMORY_BUDGET_EXHAUSTED

    await platform.acquire(uuid4(), host="sbx-b")


async def test_a_host_short_of_memory_never_evicts_another_hosts_sandbox(
    platform: Platform,
) -> None:
    """Evicting on B frees nothing on A: it would be a cold start for B's Run
    and no room for A's."""
    kept = uuid4()
    on_b = await platform.acquire(kept, host="sbx-b")
    await platform.park(kept, on_b.sandbox_id, host="sbx-b")
    await platform.acquire(uuid4(), host="sbx-a")
    await platform.acquire(uuid4(), host="sbx-a")

    with pytest.raises(SandboxRefused) as full:
        await platform.acquire(uuid4(), host="sbx-a")

    assert full.value.reason is RefusalReason.MEMORY_BUDGET_EXHAUSTED
    assert platform.state(await platform.container_of(on_b.sandbox_id)) == "paused"


async def test_a_host_refuses_a_sandbox_recorded_on_another(platform: Platform) -> None:
    """Routing sends every call to the sandbox's own host; this is what stands
    if one arrives elsewhere anyway. It must do nothing, not act on a
    container this host does not have."""
    run = uuid4()
    made = await platform.acquire(run, host="sbx-a")
    lease = platform.leases[run]

    with pytest.raises(SandboxRefused) as refusal:
        await platform.call(
            lambda c: c.freeze(run_id=run, lease_id=lease, sandbox_id=made.sandbox_id),
            host="sbx-b",
        )

    assert refusal.value.reason is RefusalReason.RESERVATION_ELSEWHERE
    assert platform.state(await platform.container_of(made.sandbox_id)) == "running"


async def test_a_run_with_a_sandbox_on_one_host_gets_none_on_another(platform: Platform) -> None:
    """A Run whose sandbox is frozen on A and whose acquire reaches B must not
    be given a second sandbox there: two sandboxes writing one workspace is
    what the isolated state exists to prevent."""
    run = uuid4()
    made = await platform.acquire(run, host="sbx-a")
    await platform.park(run, made.sandbox_id, host="sbx-a")
    before = platform.labelled()

    with pytest.raises(SandboxRefused) as refusal:
        await platform.acquire(run, host="sbx-b")

    assert refusal.value.reason is RefusalReason.RESERVATION_ELSEWHERE
    assert platform.labelled() == before
    assert platform.state(await platform.container_of(made.sandbox_id)) == "paused"


async def test_one_host_does_not_wait_on_anothers_admission(
    platform: Platform, engine: AsyncEngine
) -> None:
    """Each host's acquires serialize on their own lock. Held by A in an open
    transaction, it must not stop B from admitting."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as holding:
        await SqlSandboxStore(holding).lock_admission("sbx-a")

        await asyncio.wait_for(platform.acquire(uuid4(), host="sbx-b"), timeout=20)

        await holding.rollback()


# -- containers are created outside the admission lock (§21.3, v2.24) ---------
#
# On 2026-10-24, 120 long tasks waited 2.7 s each for the admission lock: it
# was held until the container was built, so a host built its sandboxes one
# at a time. The lock now covers counting and registering only.

CREATE_DELAY = 1.0


class SlowEngine(DockerEngine):
    """Docker taking a second to start each container."""

    async def create(self, config: Any) -> str:
        await asyncio.sleep(CREATE_DELAY)
        return await super().create(config)


class BrokenEngine(DockerEngine):
    """Docker refusing to start a container, after the volume was made."""

    async def create(self, config: Any) -> str:
        del config
        raise RuntimeError("the daemon refused the container")


class Crash(BaseException):
    """The controller process dying: nothing after it runs, no handler sees it."""


class CrashingEngine(DockerEngine):
    """The container is started, and then the controller dies."""

    async def create(self, config: Any) -> str:
        await super().create(config)
        raise Crash


async def test_two_sandboxes_are_built_at_once_rather_than_in_turn(platform: Platform) -> None:
    platform.engine_type = SlowEngine
    started = asyncio.get_running_loop().time()

    await asyncio.gather(platform.acquire(uuid4()), platform.acquire(uuid4()))

    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 2 * CREATE_DELAY - 0.2, f"{elapsed:.2f}s: built in turn, not at once"


async def test_building_outside_the_lock_still_admits_only_what_fits(platform: Platform) -> None:
    """The count and the registration stay under the lock, so two acquires
    still cannot both take the last room while their containers build."""
    platform.engine_type = SlowEngine

    outcomes = await asyncio.gather(
        platform.acquire(uuid4(), budget=256),
        platform.acquire(uuid4(), budget=256),
        return_exceptions=True,
    )

    refused = [o for o in outcomes if isinstance(o, SandboxRefused)]
    assert len(refused) == 1
    assert refused[0].reason is RefusalReason.MEMORY_BUDGET_EXHAUSTED


async def test_a_container_that_fails_to_start_leaves_nothing_and_frees_its_room(
    platform: Platform,
) -> None:
    """Its registration was committed before the build. Left behind, it would
    be a `creating` instance holding memory for ever."""
    run = uuid4()
    platform.engine_type = BrokenEngine

    with pytest.raises(RuntimeError, match="refused the container"):
        await platform.acquire(run, budget=256)

    assert await platform.committed() == 0
    assert await platform.claim_of(run) is None
    assert platform.labelled() == 0
    platform.engine_type = DockerEngine
    await platform.acquire(uuid4(), budget=256)


async def test_the_sweep_leaves_a_container_whose_acquire_is_still_finishing(
    platform: Platform, docker_client: Any
) -> None:
    """Registered before it is built, so the sweep can tell a container on
    its way from one whose acquire died."""
    run = uuid4()
    platform.engine_type = CrashingEngine
    with pytest.raises(Crash):
        await platform.acquire(run)
    sandbox = (await platform.claim_of_instance(run))
    assert (await platform.instance_of(sandbox)).status is InstanceStatus.CREATING
    platform.engine_type = DockerEngine

    await platform.call(lambda c: c.sweep())

    assert platform.labelled() == 1


async def test_cleaning_up_after_a_crash_mid_build_finds_the_container_by_its_label(
    platform: Platform,
) -> None:
    """The Scheduler's lease-expiry cleanup reaches an instance the crash left
    `creating`, with no container id written; its container still has to go."""
    run = uuid4()
    platform.engine_type = CrashingEngine
    with pytest.raises(Crash):
        await platform.acquire(run)
    sandbox = await platform.claim_of_instance(run)
    platform.engine_type = DockerEngine
    platform.expire_lease(run)

    await platform.call(lambda c: c.cleanup(run_id=run, sandbox_id=sandbox))

    assert platform.labelled() == 0
    assert (await platform.instance_of(sandbox)).status is InstanceStatus.DESTROYED
    assert await platform.committed() == 0
