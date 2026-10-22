"""What a crash leaves on the sandbox host, and who takes it away.

`acquire` starts a container before the transaction that records it commits,
so a controller that dies in between — or a commit that fails — leaves a
running container no row points at. Nothing listed containers or volumes by
label, so such a container ran, outside the memory budget, until somebody
noticed. Workspace design §13 asks for the sweep; §7 asks that a fresh
instance start on an empty data volume.

Every call is its own transaction, as the controller's dispatch makes it.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from tiny_hermes.sandbox.domain.command import SandboxCommand
from tiny_hermes.sandbox.domain.container_policy import DEFAULT_PROFILE, container_config
from tiny_hermes.sandbox.domain.models import CacheState
from tiny_hermes.sandbox.infrastructure.docker_engine import DockerEngine

from .conftest import LABEL, StubLeases
from .test_memory_admission import WORKSPACE, Platform, platform  # noqa: F401 - the fixture

__all__ = ["platform"]


async def _unrecorded(docker_client: Any, image_digest: str, run_id: UUID) -> str:
    """What `acquire` leaves when it dies after `docker run`, before commit."""
    config = container_config(
        digest=image_digest,
        profile=DEFAULT_PROFILE,
        run_id=run_id,
        instance_id=uuid4(),
        workspace_id=WORKSPACE,
        approved_digests=(image_digest,),
    )
    engine = DockerEngine(docker_client, extra_labels={LABEL: "1"})
    await engine.create_volume(f"tiny-hermes-data-{run_id}", config.volume_labels)
    return await engine.create(config)


def _volume_exists(docker_client: Any, run_id: UUID) -> bool:
    return bool(docker_client.volumes.list(filters={"name": f"tiny-hermes-data-{run_id}"}))


async def _shell(platform: Platform, run_id: UUID, sandbox_id: UUID, line: str) -> str:
    lease = platform.leases[run_id]
    answer = await platform.call(
        lambda c: c.execute(
            run_id=run_id,
            lease_id=lease,
            sandbox_id=sandbox_id,
            command=SandboxCommand(
                argv=["bash", "-c", line],
                cwd="/workspace/data",
                timeout_seconds=20,
                output_limit=4096,
            ),
        )
    )
    return answer.output


async def test_a_container_nothing_recorded_is_swept_and_a_recorded_one_is_not(
    platform: Platform, docker_client: Any, image_digest: str, leases: StubLeases
) -> None:
    lost_run = uuid4()
    lost = await _unrecorded(docker_client, image_digest, lost_run)
    leases.expire(run_id=lost_run)
    kept = await platform.acquire(uuid4())

    await platform.call(lambda c: c.sweep())

    assert platform.state(lost) is None
    assert platform.state(await platform.container_of(kept.sandbox_id)) == "running"


async def test_a_volume_whose_run_holds_nothing_is_swept(
    platform: Platform, docker_client: Any, leases: StubLeases
) -> None:
    """No claim and no lease: nothing will mount it again, and a Run that
    comes back restores from the object store onto a new one."""
    gone = uuid4()
    made = await platform.acquire(gone)
    await platform.call(
        lambda c: c.destroy(run_id=gone, lease_id=platform.leases[gone], sandbox_id=made.sandbox_id)
    )
    docker_client.volumes.create(
        name=f"tiny-hermes-data-{gone}", labels={"tiny-hermes.run": str(gone), LABEL: "1"}
    )
    leases.expire(run_id=gone)
    parked = uuid4()
    parked_box = await platform.acquire(parked)
    await platform.park(parked, parked_box.sandbox_id)

    await platform.call(lambda c: c.sweep())

    assert not _volume_exists(docker_client, gone)
    assert _volume_exists(docker_client, parked)


async def test_a_volume_whose_run_holds_a_lease_is_not_swept(
    platform: Platform, docker_client: Any
) -> None:
    """Between the lease and the claim a Worker is about to acquire on it."""
    run_id = uuid4()
    docker_client.volumes.create(
        name=f"tiny-hermes-data-{run_id}", labels={"tiny-hermes.run": str(run_id), LABEL: "1"}
    )
    platform.leases[run_id] = uuid4()

    await platform.call(lambda c: c.sweep())

    assert _volume_exists(docker_client, run_id)


async def test_a_fresh_instance_after_an_expired_keep_starts_on_an_empty_volume(
    platform: Platform,
) -> None:
    """§7: restore extracts into an empty volume. Extracting over the old one
    left files the committed revision never had, and the post-restore scan
    then failed the Run as `workspace_integrity_failed` for good."""
    run_id = uuid4()
    first = await platform.acquire(run_id)
    await _shell(platform, run_id, first.sandbox_id, "echo stale > leftover")
    lease = platform.leases[run_id]
    await platform.call(
        lambda c: c.freeze(run_id=run_id, lease_id=lease, sandbox_id=first.sandbox_id)
    )
    expired = datetime.now(UTC) - timedelta(seconds=1)
    await platform.call(
        lambda c: c.keep(run_id=run_id, sandbox_id=first.sandbox_id, until=expired)
    )

    second = await platform.acquire(run_id)

    assert second.cache_state is CacheState.RESET
    assert "leftover" not in await _shell(platform, run_id, second.sandbox_id, "ls -A")


async def test_acquire_clears_a_container_its_own_run_left_unrecorded(
    platform: Platform, docker_client: Any, image_digest: str
) -> None:
    """The Run that comes back first is the one that finds it, before any
    sweep: it would otherwise share its volume with a stranger."""
    run_id = uuid4()
    lost = await _unrecorded(docker_client, image_digest, run_id)

    await platform.acquire(run_id)

    assert platform.state(lost) is None


async def test_a_kept_instance_whose_container_is_gone_is_replaced_not_thawed(
    platform: Platform, docker_client: Any
) -> None:
    """The other half of a leak: a row with no container. An eviction whose
    transaction rolled back after the container went leaves its claim kept,
    and so does an operator who removed the container by hand. The Run's next
    slice must get a new instance, not an error from unpausing nothing."""
    run_id = uuid4()
    first = await platform.acquire(run_id)
    lease = platform.leases[run_id]
    await platform.call(
        lambda c: c.freeze(run_id=run_id, lease_id=lease, sandbox_id=first.sandbox_id)
    )
    until = datetime.now(UTC) + timedelta(minutes=5)
    await platform.call(lambda c: c.keep(run_id=run_id, sandbox_id=first.sandbox_id, until=until))
    docker_client.containers.get(await platform.container_of(first.sandbox_id)).remove(force=True)

    second = await platform.acquire(run_id)

    assert second.sandbox_id != first.sandbox_id
    assert second.cache_state is CacheState.RESET
