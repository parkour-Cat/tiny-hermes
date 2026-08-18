"""Who may move workspace bytes, decided before any byte moves.

Technical design §11.1: reaching the Controller is not authorization. Every
workspace action re-checks the lease and the reservation the way `execute`
does, plus the freeze rule §7/§8 impose — scanning a running container would
checkpoint files a background process is still changing.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from uuid import UUID

import pytest
from tiny_hermes.sandbox.application.controller import (
    RefusalReason,
    SandboxController,
    SandboxRefused,
)
from tiny_hermes.sandbox.domain.command import SandboxCommand
from tiny_hermes.sandbox.domain.models import (
    InstanceStatus,
    ReservationStatus,
    SandboxInstance,
    SandboxReservation,
)

DIGEST = "sha256:" + "d" * 64
RUN = uuid.uuid4()
LEASE = uuid.uuid4()
WORKSPACE = uuid.uuid4()
SESSION = uuid.uuid4()

COMMAND = SandboxCommand(
    argv=["echo", "hi"], cwd="/workspace/data", timeout_seconds=5, output_limit=1024
)


class FakeEngine:
    """Records what the Controller asked for, in the order it asked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.import_failure: Exception | None = None
        self.address: str | None = None

    async def create(self, config: Any) -> str:
        self.calls.append(("create", config))
        return "container-1"

    async def create_volume(self, name: str, labels: dict[str, str]) -> str:
        self.calls.append(("create_volume", (name, labels)))
        return name

    async def remove_volume(self, name: str) -> None:
        self.calls.append(("remove_volume", name))

    async def remove(self, container_id: str) -> None:
        self.calls.append(("remove", container_id))

    async def pause(self, container_id: str) -> None:
        self.calls.append(("pause", container_id))

    async def address_of(self, container_id: str) -> str | None:
        """`None` unless a test says otherwise: a container on no network has
        no address, which is what a deployment with no boundary produces."""
        self.calls.append(("address_of", container_id))
        return self.address

    async def unpause(self, container_id: str) -> None:
        self.calls.append(("unpause", container_id))

    async def import_tree(
        self, container_id: str, target: str, tar_stream: AsyncIterator[bytes]
    ) -> None:
        self.calls.append(("import_tree", (container_id, target)))
        async for _ in tar_stream:
            pass
        if self.import_failure is not None:
            raise self.import_failure

    async def scan_tree(self, container_id: str, source: str) -> tuple[Any, ...]:
        self.calls.append(("scan_tree", (container_id, source)))
        return ()

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class FakeStore:
    """The store protocol over two dictionaries."""

    def __init__(self) -> None:
        self.reservations: dict[UUID, SandboxReservation] = {}
        self.instances: dict[UUID, SandboxInstance] = {}
        #: Address -> (run, sandbox). The proxy reads this to answer "who is
        #: this" for a caller that presents nothing.
        self.addresses: dict[str, tuple[UUID, UUID]] = {}

    async def reserve(
        self, *, run_id: UUID, workspace_id: UUID, instance: SandboxInstance
    ) -> SandboxReservation:
        self.instances[instance.id] = instance
        claim = SandboxReservation(
            id=uuid.uuid4(),
            run_id=run_id,
            workspace_id=workspace_id,
            instance_id=instance.id,
            status=ReservationStatus.ACTIVE,
        )
        self.reservations[claim.id] = claim
        return claim

    async def live_for_run(self, run_id: UUID) -> SandboxReservation | None:
        for claim in self.reservations.values():
            if claim.run_id == run_id and claim.status is not ReservationStatus.RELEASED:
                return claim
        return None

    async def read(self, reservation_id: UUID) -> SandboxReservation | None:
        return self.reservations.get(reservation_id)

    async def keep(
        self, reservation_id: UUID, *, idle_expires_at: datetime
    ) -> SandboxReservation:
        return self._update(reservation_id, ReservationStatus.KEPT, idle_expires_at)

    async def isolate(self, reservation_id: UUID, *, reason: str) -> SandboxReservation:
        claim = self.reservations[reservation_id]
        updated = SandboxReservation(
            id=claim.id,
            run_id=claim.run_id,
            workspace_id=claim.workspace_id,
            instance_id=claim.instance_id,
            status=ReservationStatus.ISOLATED,
            isolation_reason=reason,
        )
        self.reservations[reservation_id] = updated
        return updated

    async def release(self, reservation_id: UUID) -> SandboxReservation:
        return self._update(reservation_id, ReservationStatus.RELEASED, None)

    async def expired_keeps(self, now: datetime) -> list[SandboxReservation]:
        del now
        return []

    async def isolated(self, limit: int) -> list[SandboxReservation]:
        del limit
        return []

    async def read_instance(self, instance_id: UUID) -> SandboxInstance | None:
        return self.instances.get(instance_id)

    async def register_egress_address(
        self, *, address: str, run_id: UUID, sandbox_id: UUID
    ) -> None:
        self.addresses[address] = (run_id, sandbox_id)

    async def clear_egress_address(self, sandbox_id: UUID) -> None:
        for address, (_, owner) in list(self.addresses.items()):
            if owner == sandbox_id:
                del self.addresses[address]

    async def set_instance_status(
        self, instance_id: UUID, status: InstanceStatus
    ) -> SandboxInstance:
        stale = self.instances[instance_id]
        fresh = SandboxInstance(
            id=stale.id,
            container_id=stale.container_id,
            image_digest=stale.image_digest,
            resource_profile=stale.resource_profile,
            boot_id=stale.boot_id,
            status=status,
        )
        self.instances[instance_id] = fresh
        return fresh

    def _update(
        self,
        reservation_id: UUID,
        status: ReservationStatus,
        idle_expires_at: datetime | None,
    ) -> SandboxReservation:
        claim = self.reservations[reservation_id]
        updated = SandboxReservation(
            id=claim.id,
            run_id=claim.run_id,
            workspace_id=claim.workspace_id,
            instance_id=claim.instance_id,
            status=status,
            idle_expires_at=idle_expires_at,
        )
        self.reservations[reservation_id] = updated
        return updated


class ScriptedLeases:
    def __init__(self) -> None:
        self.valid = True
        self.live = True

    async def holds(self, run_id: UUID, lease_id: UUID) -> bool:
        del run_id, lease_id
        return self.valid

    async def any_live(self, run_id: UUID) -> bool:
        del run_id
        return self.live


async def _tar() -> AsyncIterator[bytes]:
    yield b"tar bytes"


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def leases() -> ScriptedLeases:
    return ScriptedLeases()


@pytest.fixture
def controller(engine: FakeEngine, leases: ScriptedLeases) -> SandboxController:
    return SandboxController(
        engine=engine,  # type: ignore[arg-type] - a recording double
        store=FakeStore(),
        approved_digests=(DIGEST,),
        leases=leases,
    )


async def _acquired(controller: SandboxController) -> UUID:
    answer = await controller.acquire(
        run_id=RUN,
        lease_id=LEASE,
        workspace_id=WORKSPACE,
        session_id=SESSION,
        profile="default",
    )
    return answer.sandbox_id


async def _frozen(controller: SandboxController) -> UUID:
    sandbox_id = await _acquired(controller)
    await controller.freeze(run_id=RUN, lease_id=LEASE, sandbox_id=sandbox_id)
    return sandbox_id


async def test_acquire_creates_the_labelled_volume_before_the_container(
    controller: SandboxController, engine: FakeEngine
) -> None:
    await _acquired(controller)

    assert engine.names()[:2] == ["create_volume", "create"]
    name, labels = engine.calls[0][1]
    assert name == f"tiny-hermes-data-{RUN}"
    assert labels["tiny-hermes.run"] == str(RUN)
    assert labels["tiny-hermes.workspace"] == str(WORKSPACE)
    assert labels["tiny-hermes.session"] == str(SESSION)


async def test_destroy_removes_the_container_and_then_the_volume(
    controller: SandboxController, engine: FakeEngine
) -> None:
    sandbox_id = await _acquired(controller)
    await controller.destroy(run_id=RUN, lease_id=LEASE, sandbox_id=sandbox_id)

    assert engine.names()[-2:] == ["remove", "remove_volume"]
    assert engine.calls[-1][1] == f"tiny-hermes-data-{RUN}"


async def test_workspace_actions_require_a_live_matching_lease(
    controller: SandboxController, leases: ScriptedLeases
) -> None:
    sandbox_id = await _frozen(controller)
    leases.valid = False

    with pytest.raises(SandboxRefused) as refused:
        await controller.workspace_import(
            run_id=RUN, lease_id=LEASE, sandbox_id=sandbox_id, declared_total=10
        )
    assert refused.value.reason is RefusalReason.LEASE_INVALID
    with pytest.raises(SandboxRefused):
        await controller.workspace_scan(
            run_id=RUN, lease_id=LEASE, sandbox_id=sandbox_id
        )
    with pytest.raises(SandboxRefused):
        await controller.workspace_export(
            run_id=RUN, lease_id=LEASE, sandbox_id=sandbox_id
        )


async def test_import_scan_export_require_a_frozen_instance(
    controller: SandboxController,
) -> None:
    """NOT_FROZEN: a running container's files are still being changed."""
    sandbox_id = await _acquired(controller)

    for attempt in ("import", "scan", "export"):
        with pytest.raises(SandboxRefused) as refused:
            if attempt == "import":
                await controller.workspace_import(
                    run_id=RUN, lease_id=LEASE, sandbox_id=sandbox_id, declared_total=10
                )
            elif attempt == "scan":
                await controller.workspace_scan(
                    run_id=RUN, lease_id=LEASE, sandbox_id=sandbox_id
                )
            else:
                await controller.workspace_export(
                    run_id=RUN, lease_id=LEASE, sandbox_id=sandbox_id
                )
        assert refused.value.reason is RefusalReason.NOT_FROZEN, attempt


async def test_an_interrupted_import_marks_the_instance_dirty(
    controller: SandboxController, engine: FakeEngine
) -> None:
    sandbox_id = await _frozen(controller)

    await controller.workspace_import_failed(run_id=RUN, sandbox_id=sandbox_id)

    instance = await controller.store.read_instance(sandbox_id)
    assert instance is not None and instance.status is InstanceStatus.ISOLATED
    claim = await controller.store.live_for_run(RUN)
    assert claim is not None and claim.status is ReservationStatus.ISOLATED
    del engine


async def test_import_into_a_dirty_instance_is_refused_without_reset(
    controller: SandboxController,
) -> None:
    sandbox_id = await _frozen(controller)
    await controller.workspace_import_failed(run_id=RUN, sandbox_id=sandbox_id)

    with pytest.raises(SandboxRefused) as refused:
        await controller.workspace_import(
            run_id=RUN, lease_id=LEASE, sandbox_id=sandbox_id, declared_total=10
        )
    assert refused.value.reason is RefusalReason.INSTANCE_DIRTY


async def test_a_verified_import_hands_bytes_to_the_engine(
    controller: SandboxController, engine: FakeEngine
) -> None:
    sandbox_id = await _frozen(controller)

    handle = await controller.workspace_import(
        run_id=RUN, lease_id=LEASE, sandbox_id=sandbox_id, declared_total=10
    )
    await controller.engine.import_tree(handle.container_id, "/workspace/data", _tar())

    assert ("import_tree", ("container-1", "/workspace/data")) in engine.calls


async def test_execute_stream_requires_a_running_instance_and_allowed_cwd(
    controller: SandboxController,
) -> None:
    sandbox_id = await _frozen(controller)
    with pytest.raises(SandboxRefused) as refused:
        await controller.execute_stream(
            run_id=RUN, lease_id=LEASE, sandbox_id=sandbox_id, command=COMMAND
        )
    assert refused.value.reason is RefusalReason.NOT_RUNNING

    await controller.thaw(run_id=RUN, lease_id=LEASE, sandbox_id=sandbox_id)
    hostile = SandboxCommand(
        argv=["cat", "/etc/passwd"], cwd="/etc", timeout_seconds=5, output_limit=64
    )
    with pytest.raises(SandboxRefused) as outside:
        await controller.execute_stream(
            run_id=RUN, lease_id=LEASE, sandbox_id=sandbox_id, command=hostile
        )
    assert outside.value.reason is RefusalReason.WORKING_DIRECTORY_NOT_ALLOWED


async def test_volume_remove_uses_scheduler_authority_rules(
    controller: SandboxController, leases: ScriptedLeases, engine: FakeEngine
) -> None:
    """No live lease, no live claim still executing, server-recorded IDs only."""
    sandbox_id = await _acquired(controller)

    with pytest.raises(SandboxRefused) as live:
        await controller.volume_remove(run_id=RUN, sandbox_id=sandbox_id)
    assert live.value.reason is RefusalReason.LEASE_STILL_LIVE

    leases.live = False
    with pytest.raises(SandboxRefused) as active:
        await controller.volume_remove(run_id=RUN, sandbox_id=sandbox_id)
    assert active.value.reason is RefusalReason.RESERVATION_STILL_LIVE

    claim = await controller.store.live_for_run(RUN)
    assert claim is not None
    await controller.store.isolate(claim.id, reason="cleanup drill")
    await controller.volume_remove(run_id=RUN, sandbox_id=sandbox_id)
    assert engine.calls[-1] == ("remove_volume", f"tiny-hermes-data-{RUN}")


# -- the identity a sandbox holds, and for exactly how long ------------------


async def test_a_sandbox_holds_its_identity_only_while_it_may_use_it(
    engine: FakeEngine, leases: ScriptedLeases
) -> None:
    """§16.4: a frozen instance may not open a new connection.

    And the case that would be a hole rather than an inconvenience: a destroyed
    container must not lend its identity to whatever Docker hands the address
    to next, so the row goes before the container does.
    """
    engine.address = "172.30.0.5"
    store = FakeStore()
    controller = SandboxController(
        engine=engine,  # type: ignore[arg-type] - a recording double
        store=store,
        approved_digests=(DIGEST,),
        leases=leases,
    )

    sandbox_id = await _acquired(controller)
    assert store.addresses == {"172.30.0.5": (RUN, sandbox_id)}

    await controller.freeze(run_id=RUN, lease_id=LEASE, sandbox_id=sandbox_id)
    assert store.addresses == {}

    await controller.thaw(run_id=RUN, lease_id=LEASE, sandbox_id=sandbox_id)
    assert store.addresses == {"172.30.0.5": (RUN, sandbox_id)}

    await controller.destroy(run_id=RUN, lease_id=LEASE, sandbox_id=sandbox_id)
    assert store.addresses == {}


async def test_a_sandbox_with_no_network_is_registered_as_nobody(
    engine: FakeEngine, leases: ScriptedLeases
) -> None:
    """A container on no network has no address to write down, and nothing
    will ever ask who it is — it cannot reach the proxy to be asked."""
    engine.address = None
    store = FakeStore()
    controller = SandboxController(
        engine=engine,  # type: ignore[arg-type] - a recording double
        store=store,
        approved_digests=(DIGEST,),
        leases=leases,
    )

    await _acquired(controller)

    assert store.addresses == {}
