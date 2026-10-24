"""The Controller, against a real Docker daemon.

Refusals first. Technical design §11.1 says `仅能连接 Unix socket 不能替代这些
校验` — reaching the Controller is not authorization, because both the Worker
and the Scheduler can reach it and they act on different Runs. Those checks are
the Controller's actual job, so they are what gets written first and what gets
the most tests.

Container properties are asserted from `docker inspect`, never from the
configuration handed to Docker. The pure policy already proves the platform
builds the right dict; only the daemon can say what it built.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Network, ip_address
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.egress.infrastructure.sql_directory import SqlScopeDirectory
from tiny_hermes.sandbox.application.controller import (
    AcquireResult,
    RefusalReason,
    SandboxController,
    SandboxRefused,
)
from tiny_hermes.sandbox.domain.command import SandboxCommand
from tiny_hermes.sandbox.domain.container_policy import SandboxNetworking
from tiny_hermes.sandbox.domain.models import CacheState, InstanceStatus
from tiny_hermes.sandbox.infrastructure.docker_engine import ContainerNotRunning, DockerEngine
from tiny_hermes.sandbox.infrastructure.sql_store import SqlSandboxStore

from ..conftest import VALID_SPEC
from .conftest import LABEL, RecordingAudit, StubLeases

RUN = uuid4()
LEASE = uuid4()
WORKSPACE = uuid4()


def command(argv: list[str], **overrides: Any) -> SandboxCommand:
    fields: dict[str, Any] = {
        "argv": argv,
        "cwd": "/workspace/data",
        "timeout_seconds": 30,
        "output_limit": 65_536,
    }
    fields.update(overrides)
    return SandboxCommand(**fields)


async def acquired(
    controller: SandboxController, run_id: UUID = RUN, lease: UUID = LEASE
) -> AcquireResult:
    return await controller.acquire(
        run_id=run_id, lease_id=lease, workspace_id=WORKSPACE, profile="default"
    )


# --------------------------------------------------------------------------
# Ownership. These are the rules; everything else is plumbing.
# --------------------------------------------------------------------------


async def test_acquiring_twice_for_one_run_is_refused_and_creates_nothing(
    controller: SandboxController, docker_client: Any
) -> None:
    """Counted on the daemon's side, because a container created and then
    abandoned is exactly the leak this refusal prevents."""
    await acquired(controller)
    before = len(docker_client.containers.list(all=True))

    with pytest.raises(SandboxRefused) as refusal:
        await acquired(controller)

    assert refusal.value.reason is RefusalReason.ALREADY_RESERVED
    assert len(docker_client.containers.list(all=True)) == before


async def test_a_lease_belonging_to_another_run_is_refused(
    controller: SandboxController, leases: StubLeases
) -> None:
    box = await acquired(controller)
    leases.deny(run_id=RUN, lease_id=LEASE)

    with pytest.raises(SandboxRefused) as refusal:
        await controller.execute(
            run_id=RUN, lease_id=LEASE, sandbox_id=box.sandbox_id, command=command(["true"])
        )

    assert refusal.value.reason is RefusalReason.LEASE_INVALID


async def test_a_sandbox_belonging_to_another_run_is_refused(
    controller: SandboxController,
) -> None:
    """The `sandbox_id` is the caller's input, so it is checked and not trusted.

    Both Runs hold a sandbox, which is the dangerous shape: a caller with a
    perfectly valid lease and a perfectly valid reservation, naming somebody
    else's container.
    """
    mine = await acquired(controller)
    other = uuid4()
    theirs = await acquired(controller, run_id=other, lease=uuid4())
    assert theirs.sandbox_id != mine.sandbox_id

    with pytest.raises(SandboxRefused) as refusal:
        await controller.execute(
            run_id=other, lease_id=LEASE, sandbox_id=mine.sandbox_id, command=command(["true"])
        )

    assert refusal.value.reason is RefusalReason.RESERVATION_NOT_OWNED


async def test_executing_without_a_reservation_is_refused(
    controller: SandboxController,
) -> None:
    with pytest.raises(SandboxRefused) as refusal:
        await controller.execute(
            run_id=RUN, lease_id=LEASE, sandbox_id=uuid4(), command=command(["true"])
        )

    assert refusal.value.reason is RefusalReason.NO_RESERVATION


async def test_executing_after_a_freeze_is_refused(controller: SandboxController) -> None:
    """A frozen instance gets no CPU. Executing in one would either hang or
    silently thaw it, and both are worse than saying no."""
    box = await acquired(controller)
    await controller.freeze(run_id=RUN, lease_id=LEASE, sandbox_id=box.sandbox_id)

    with pytest.raises(SandboxRefused) as refusal:
        await controller.execute(
            run_id=RUN, lease_id=LEASE, sandbox_id=box.sandbox_id, command=command(["true"])
        )

    assert refusal.value.reason is RefusalReason.NOT_RUNNING


async def test_scheduler_cleanup_is_refused_while_the_lease_is_live(
    controller: SandboxController, docker_client: Any
) -> None:
    """The Scheduler's authority is for after a lease expires.

    Without this check it could destroy the container of a Run that is at that
    moment executing in it.
    """
    box = await acquired(controller)

    with pytest.raises(SandboxRefused) as refusal:
        await controller.cleanup(run_id=RUN, sandbox_id=box.sandbox_id)

    assert refusal.value.reason is RefusalReason.LEASE_STILL_LIVE
    assert docker_client.containers.get(await _container_of(controller, box.sandbox_id))


async def test_scheduler_cleanup_after_expiry_destroys_and_audits(
    controller: SandboxController,
    docker_client: Any,
    leases: StubLeases,
    audit: RecordingAudit,
) -> None:
    box = await acquired(controller)
    container = await _container_of(controller, box.sandbox_id)
    leases.expire(run_id=RUN)

    await controller.cleanup(run_id=RUN, sandbox_id=box.sandbox_id)

    assert container not in {c.id for c in docker_client.containers.list(all=True)}
    # Two entries since 3C: the data volume's reclamation is a material
    # deletion of its own (design §13), confirmed separately from the sandbox.
    assert [entry.action for entry in audit.entries] == [
        "sandbox.volume_remove",
        "sandbox.cleanup",
    ]
    assert audit.entries[0].run_id == RUN


# --------------------------------------------------------------------------
# What Docker actually built.
# --------------------------------------------------------------------------


async def test_the_container_the_daemon_built_matches_the_policy(
    controller: SandboxController, docker_client: Any
) -> None:
    box = await acquired(controller)
    found = docker_client.containers.get(await _container_of(controller, box.sandbox_id))
    config, host = found.attrs["Config"], found.attrs["HostConfig"]

    assert config["User"] == "10001:10001"
    assert host["ReadonlyRootfs"] is True
    assert host["NetworkMode"] == "none"
    assert host["CapDrop"] == ["ALL"]
    assert "no-new-privileges:true" in host["SecurityOpt"]
    assert host["NanoCpus"] == 1_000_000_000
    assert host["Memory"] == 1024 * 1024 * 1024
    assert host["PidsLimit"] == 128
    assert host["Init"] is True
    # Nothing that would weaken the rest.
    assert host["Privileged"] is False
    assert host["Binds"] in (None, [])
    assert host["Devices"] in (None, [])


async def test_nothing_inside_the_sandbox_can_reach_a_network(
    controller: SandboxController,
) -> None:
    """Asserted on the absence of an interface, not on a tool failing.

    `curl: could not resolve` would also be produced by a broken DNS config.
    One loopback line in /proc/net/dev is the network not existing.
    """
    box = await acquired(controller)
    answer = await controller.execute(
        run_id=RUN,
        lease_id=LEASE,
        sandbox_id=box.sandbox_id,
        command=command(["cat", "/proc/net/dev"]),
    )

    interfaces = [
        line.split(":")[0].strip()
        for line in answer.output.splitlines()
        if ":" in line and not line.strip().startswith(("Inter", "face"))
    ]
    assert interfaces == ["lo"]


async def test_a_name_cannot_be_resolved(controller: SandboxController) -> None:
    box = await acquired(controller)
    answer = await controller.execute(
        run_id=RUN,
        lease_id=LEASE,
        sandbox_id=box.sandbox_id,
        command=command(["getent", "hosts", "example.com"]),
    )
    assert answer.exit_code != 0


async def test_the_root_filesystem_cannot_be_written_and_the_workspace_can(
    controller: SandboxController,
) -> None:
    box = await acquired(controller)

    refused = await controller.execute(
        run_id=RUN,
        lease_id=LEASE,
        sandbox_id=box.sandbox_id,
        command=command(["touch", "/etc/planted"]),
    )
    allowed = await controller.execute(
        run_id=RUN,
        lease_id=LEASE,
        sandbox_id=box.sandbox_id,
        command=command(["touch", "/workspace/data/notes"]),
    )

    assert refused.exit_code != 0
    assert allowed.exit_code == 0


async def test_two_commands_in_one_slice_run_in_the_same_container(
    controller: SandboxController,
) -> None:
    """The roadmap's phase-three check: 同一时间片多工具步骤不重建容器.

    Asked of the sandbox itself rather than of the platform's bookkeeping — a
    file written by the first command is there for the second only if it really
    is one container.
    """
    box = await acquired(controller)
    await controller.execute(
        run_id=RUN,
        lease_id=LEASE,
        sandbox_id=box.sandbox_id,
        command=command(["touch", "/workspace/cache/marker"]),
    )
    answer = await controller.execute(
        run_id=RUN,
        lease_id=LEASE,
        sandbox_id=box.sandbox_id,
        command=command(["ls", "/workspace/cache"]),
    )

    assert "marker" in answer.output


async def test_a_command_that_outruns_its_timeout_is_reported_not_raised(
    controller: SandboxController,
) -> None:
    """§11.5 puts this decision in the loop, so it comes back as a result."""
    box = await acquired(controller)
    answer = await controller.execute(
        run_id=RUN,
        lease_id=LEASE,
        sandbox_id=box.sandbox_id,
        command=command(["sleep", "10"], timeout_seconds=1),
    )

    assert answer.timed_out is True
    assert answer.exit_code != 0


async def test_output_beyond_the_limit_is_truncated_and_says_so(
    controller: SandboxController,
) -> None:
    box = await acquired(controller)
    answer = await controller.execute(
        run_id=RUN,
        lease_id=LEASE,
        sandbox_id=box.sandbox_id,
        command=command(["seq", "1", "100000"], output_limit=2_048),
    )

    assert answer.truncated is True
    assert len(answer.output.encode()) <= 2_048 + 200


#: Lists every process in the container by its command line. The runtime
#: image has no `ps`, and adding one for a test would hand it to every Agent.
_PROCESSES = (
    "for p in /proc/[0-9]*; do tr '\\0' ' ' < $p/cmdline 2>/dev/null; echo; done"
)


async def _stops(
    controller: SandboxController, sandbox_id: UUID, marker: str, within: float = 10.0
) -> bool:
    """Whether the processes are gone within `within` seconds.

    Asked until they are, not once after a fixed second: stopping a process
    group on a busy Docker host took longer than that once (2026-10-23), and
    the test failed with nothing wrong. Still bounded, so a command that is
    never stopped fails here.
    """
    deadline = asyncio.get_running_loop().time() + within
    while await _still_running(controller, sandbox_id, marker):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.25)
    return True


async def _still_running(controller: SandboxController, sandbox_id: UUID, marker: str) -> bool:
    listing = await controller.execute(
        run_id=RUN,
        lease_id=LEASE,
        sandbox_id=sandbox_id,
        command=command(["bash", "-c", _PROCESSES]),
    )
    return any(marker in line and "/proc" not in line for line in listing.output.splitlines())


async def test_a_command_that_outruns_its_timeout_is_stopped_with_what_it_started(
    controller: SandboxController,
) -> None:
    """The model is told the command "was stopped". A `npm run dev` left
    running would keep its CPU and pids, be frozen with the sandbox at the
    slice end and wake up with it, and keep writing into the workspace."""
    box = await acquired(controller)
    answer = await controller.execute(
        run_id=RUN,
        lease_id=LEASE,
        sandbox_id=box.sandbox_id,
        command=command(["bash", "-c", "sleep 1001 & sleep 1000"], timeout_seconds=2),
    )
    assert answer.timed_out is True

    assert await _stops(controller, box.sandbox_id, "sleep 100")


async def test_a_streamed_command_that_outruns_its_timeout_is_stopped_too(
    controller: SandboxController,
) -> None:
    box = await acquired(controller)
    container_id = await _container_of(controller, box.sandbox_id)

    class Sink:
        artifact_limit = 4096

        async def deliver(self, chunk: bytes) -> None:
            del chunk

    result = await controller.engine.execute_streamed(
        container_id,
        command(["bash", "-c", "sleep 1003 & sleep 1002"], timeout_seconds=2),
        Sink(),
    )
    assert result.timed_out is True

    assert await _stops(controller, box.sandbox_id, "sleep 100")


async def test_a_timed_out_command_keeps_what_it_printed(
    controller: SandboxController,
) -> None:
    """What a command printed before it hung is usually why it hung."""
    box = await acquired(controller)
    answer = await controller.execute(
        run_id=RUN,
        lease_id=LEASE,
        sandbox_id=box.sandbox_id,
        command=command(["bash", "-c", "echo got-this-far; sleep 1000"], timeout_seconds=2),
    )

    assert answer.timed_out is True
    assert "got-this-far" in answer.output


async def test_output_past_the_limit_is_cut_while_it_is_read(
    controller: SandboxController,
) -> None:
    """A command that never stops printing must not grow the controller's
    memory until the timeout: it is cut at the limit and the rest drained."""
    box = await acquired(controller)
    answer = await controller.execute(
        run_id=RUN,
        lease_id=LEASE,
        sandbox_id=box.sandbox_id,
        command=command(["yes"], timeout_seconds=2, output_limit=2_048),
    )

    assert answer.timed_out is True
    assert answer.truncated is True
    assert 0 < len(answer.output.encode()) <= 2_048 + 200


# --------------------------------------------------------------------------
# Lifecycle.
# --------------------------------------------------------------------------


async def test_a_fresh_sandbox_reports_its_cache_was_reset(
    controller: SandboxController,
) -> None:
    assert (await acquired(controller)).cache_state is CacheState.RESET


async def test_thawing_this_runs_own_instance_reports_reuse(
    controller: SandboxController,
) -> None:
    first = await acquired(controller)
    await controller.freeze(run_id=RUN, lease_id=LEASE, sandbox_id=first.sandbox_id)
    await controller.keep(
        run_id=RUN, sandbox_id=first.sandbox_id, until=datetime.now(UTC) + timedelta(minutes=5)
    )

    second = await acquired(controller, lease=uuid4())

    assert second.sandbox_id == first.sandbox_id
    assert second.cache_state is CacheState.REUSED


async def test_a_reused_instance_is_somebody_to_the_proxy_again(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    docker_client: Any,
    image_digest: str,
    leases: StubLeases,
    proxy: Any,
) -> None:
    """The proxy knows a sandbox only by its address row. A slice ends with
    freeze, which deletes the row, and the next slice's acquire hands the
    same container back: the proxy must know it again, at the address Docker
    actually gave the container."""
    run_id = UUID(_a_run(client, scope))
    networking = SandboxNetworking(
        proxy_url="http://egress-proxy:3128",
        proxy_container=proxy.name,
        pool=IPv4Network("10.249.0.0/16"),
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            controller = SandboxController(
                engine=DockerEngine(docker_client, extra_labels={LABEL: "1"}),
                store=SqlSandboxStore(session),
                approved_digests=(image_digest,),
                leases=leases,
                networking=networking,
            )
            first = await acquired(controller, run_id=run_id)
            await controller.freeze(run_id=run_id, lease_id=LEASE, sandbox_id=first.sandbox_id)
            await controller.keep(
                run_id=run_id,
                sandbox_id=first.sandbox_id,
                until=datetime.now(UTC) + timedelta(minutes=5),
            )
            await session.commit()

            second = await acquired(controller, run_id=run_id, lease=uuid4())
            await session.commit()
            assert second.cache_state is CacheState.REUSED
            container = docker_client.containers.get(
                await _container_of(controller, second.sandbox_id)
            )

        network_name = networking.network_name(second.sandbox_id)
        given = container.attrs["NetworkSettings"]["Networks"][network_name]["IPAddress"]
        directory = SqlScopeDirectory(sessions)
        claim = await directory.sandbox_claim(ip_address(given))
        assert claim is not None
        assert claim.run_id == run_id
    finally:
        for leftover in docker_client.containers.list(all=True, filters={"label": f"{LABEL}=1"}):
            if leftover.id != proxy.id:
                leftover.remove(force=True)
        for leftover in docker_client.networks.list(filters={"label": f"{LABEL}=1"}):
            leftover.reload()
            for attached in leftover.attrs.get("Containers", {}):
                leftover.disconnect(attached, force=True)
            leftover.remove()


def _a_run(client: TestClient, scope: dict[str, str]) -> str:
    """A Run for the address row to belong to; its Agent asks for nothing."""
    agent_id = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Runner", "alias": "runner"}
    ).json()["id"]
    draft = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": VALID_SPEC},
    )
    assert draft.status_code == 200, draft.text
    published = client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=scope,
        json={"expected_revision": draft.json()["revision"]},
    )
    assert published.status_code == 201, published.text
    session_id = client.post("/api/v1/sessions", headers=scope, json={"agent_id": agent_id}).json()[
        "id"
    ]
    created = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": "reused-instance"},
        json={"session_id": session_id, "input": "hello"},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def test_a_kept_instance_past_its_deadline_is_not_reused(
    controller: SandboxController,
) -> None:
    """§11.3: only a thaw inside the TTL is `reused`. Anything else is a new
    writable layer, and the Agent is told so."""
    first = await acquired(controller)
    await controller.freeze(run_id=RUN, lease_id=LEASE, sandbox_id=first.sandbox_id)
    await controller.keep(
        run_id=RUN, sandbox_id=first.sandbox_id, until=datetime.now(UTC) - timedelta(seconds=1)
    )

    second = await acquired(controller, lease=uuid4())

    assert second.sandbox_id != first.sandbox_id
    assert second.cache_state is CacheState.RESET


async def test_freeze_stops_the_container_and_thaw_starts_it(
    controller: SandboxController, docker_client: Any
) -> None:
    box = await acquired(controller)
    name = await _container_of(controller, box.sandbox_id)

    await controller.freeze(run_id=RUN, lease_id=LEASE, sandbox_id=box.sandbox_id)
    docker_client.containers.get(name).reload()
    assert docker_client.containers.get(name).status == "paused"

    await controller.thaw(run_id=RUN, lease_id=LEASE, sandbox_id=box.sandbox_id)
    assert docker_client.containers.get(name).status == "running"


async def test_freezing_a_container_that_died_says_so_rather_than_blaming_docker(
    controller: SandboxController, docker_client: Any
) -> None:
    # A sandbox past its memory with nothing left to kill, or killed from
    # outside, is still there but no longer running. The daemon is fine, and
    # an operator reading "DockerUnavailable" goes looking at the wrong thing.
    box = await acquired(controller)
    name = await _container_of(controller, box.sandbox_id)
    docker_client.containers.get(name).kill()

    with pytest.raises(ContainerNotRunning):
        await controller.freeze(run_id=RUN, lease_id=LEASE, sandbox_id=box.sandbox_id)


async def test_destroy_removes_the_container_and_ends_the_reservation(
    controller: SandboxController, docker_client: Any
) -> None:
    box = await acquired(controller)
    name = await _container_of(controller, box.sandbox_id)

    await controller.destroy(run_id=RUN, lease_id=LEASE, sandbox_id=box.sandbox_id)

    assert name not in {c.id for c in docker_client.containers.list(all=True)}
    # And the Run may reserve again, which is what `released` means.
    again = await acquired(controller)
    assert again.sandbox_id != box.sandbox_id


async def test_inspect_reports_state_without_changing_it(
    controller: SandboxController,
) -> None:
    box = await acquired(controller)
    seen = await controller.inspect(run_id=RUN, sandbox_id=box.sandbox_id)
    assert seen.status is InstanceStatus.RUNNING

    await controller.freeze(run_id=RUN, lease_id=LEASE, sandbox_id=box.sandbox_id)
    assert (await controller.inspect(run_id=RUN, sandbox_id=box.sandbox_id)).status is (
        InstanceStatus.FROZEN
    )


async def test_a_deployment_with_no_approved_image_cannot_start_a_sandbox(
    controller: SandboxController, docker_client: Any
) -> None:
    """The reachable half of §11.3's image rule.

    A caller cannot name an unapproved image because a caller cannot name an
    image at all — the Controller reads it from the allowlist, so the only way
    to get a wrong one is to approve it. What remains reachable is the empty
    allowlist, which is the default before an operator configures one, and it
    must fail closed rather than reach for a latest tag.
    """
    controller.approved_digests = ()
    before = len(docker_client.containers.list(all=True))

    with pytest.raises(SandboxRefused) as refusal:
        await acquired(controller)

    assert refusal.value.reason is RefusalReason.IMAGE_NOT_APPROVED
    assert len(docker_client.containers.list(all=True)) == before


async def _container_of(controller: SandboxController, sandbox_id: UUID) -> str:
    found = await controller.store.read_instance(sandbox_id)
    assert found is not None and found.container_id is not None
    return found.container_id


async def test_two_commands_on_one_sandbox_run_at_once_and_each_gets_its_own_output(
    controller: SandboxController,
) -> None:
    """What the Worker's parallel reads rely on: two executes on the same
    sandbox at the same time neither serialize nor mix their outputs.
    `test_two_clients_are_served_at_once` (transport) proves the socket does
    not queue them; this is the Controller and the daemon under it."""
    box = await acquired(controller)
    for name, body in (("a.txt", "alpha"), ("b.txt", "bravo")):
        await controller.execute(
            run_id=RUN,
            lease_id=LEASE,
            sandbox_id=box.sandbox_id,
            command=command(["sh", "-c", f"printf {body} > /workspace/data/{name}"]),
        )

    loop = asyncio.get_running_loop()
    started = loop.time()
    first, second = await asyncio.gather(
        controller.execute(
            run_id=RUN,
            lease_id=LEASE,
            sandbox_id=box.sandbox_id,
            command=command(["sh", "-c", "sleep 1; cat /workspace/data/a.txt"]),
        ),
        controller.execute(
            run_id=RUN,
            lease_id=LEASE,
            sandbox_id=box.sandbox_id,
            command=command(["sh", "-c", "sleep 1; cat /workspace/data/b.txt"]),
        ),
    )
    elapsed = loop.time() - started

    assert first.output == "alpha"
    assert second.output == "bravo"
    assert elapsed < 1.8, f"the two executes were serialized ({elapsed:.2f}s)"
