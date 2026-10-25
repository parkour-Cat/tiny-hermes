"""Who may do what to which container.

Technical design §11.1: `仅能连接 Unix socket 不能替代这些校验`. Reaching this
object is not authorization. Both the Worker and the Scheduler can reach it and
they act on different Runs, so every action re-checks that the Reservation
belongs to the Run being named, and — for a Worker action — that the lease being
presented is that Run's and is still valid.

The Scheduler acts under a separate authority and only after a lease has
expired. Letting it act while a lease is live would let it destroy the container
of a Run that is at that moment executing in it.
"""

import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from ipaddress import IPv4Network
from typing import Protocol
from uuid import UUID

from tiny_hermes.sandbox.domain.command import (
    ALLOWED_WORKING_DIRECTORIES,
    CommandResult,
    SandboxCommand,
    ScannedEntry,
)
from tiny_hermes.sandbox.domain.container_policy import (
    DEFAULT_PROFILE,
    MIN_SANDBOX_MEMORY_MB,
    ContainerPolicyError,
    ResourceProfile,
    SandboxNetworking,
    container_config,
    profile_named,
)
from tiny_hermes.sandbox.domain.models import (
    CacheState,
    InstanceStatus,
    ReservationStatus,
    SandboxInstance,
    SandboxReservation,
)
from tiny_hermes.sandbox.infrastructure.docker_engine import (
    ContainerInfo,
    DockerEngine,
    DockerUnavailable,
    SubnetTaken,
)
from tiny_hermes.sandbox.ports.store import SandboxStore
from tiny_hermes.shared.metrics import SANDBOX_ADMISSION_REJECTED

logger = logging.getLogger(__name__)


class RefusalReason(StrEnum):
    NO_RESERVATION = "no_reservation"
    RESERVATION_NOT_OWNED = "reservation_not_owned"
    ALREADY_RESERVED = "already_reserved"
    LEASE_INVALID = "lease_invalid"
    LEASE_STILL_LIVE = "lease_still_live"
    RESERVATION_STILL_LIVE = "reservation_still_live"
    NOT_RUNNING = "not_running"
    NOT_FROZEN = "not_frozen"
    INSTANCE_DIRTY = "instance_dirty"
    IMAGE_NOT_APPROVED = "image_not_approved"
    #: §21.3: the new instance's limit does not fit, even after evicting the
    #: frozen instances that could go. The Run waits; it has not failed.
    MEMORY_BUDGET_EXHAUSTED = "memory_budget_exhausted"
    #: §21.3, v2.22: an Agent's declared size is below the floor, above the
    #: ceiling, or larger than the whole budget. None of those ever fits, so
    #: the Run fails rather than queueing to be refused again.
    MEMORY_OUT_OF_RANGE = "memory_out_of_range"
    NETWORK_POOL_EXHAUSTED = "network_pool_exhausted"
    #: Tier C: the sandbox, or the Run's live claim, is recorded on another
    #: sandbox host. This host has no such container and does nothing.
    RESERVATION_ELSEWHERE = "reservation_elsewhere"
    WORKING_DIRECTORY_NOT_ALLOWED = "working_directory_not_allowed"


class SandboxRefused(Exception):
    def __init__(self, reason: RefusalReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class SandboxUnreachable(Exception):
    """No answer came back from the Controller, so nothing was decided.

    The opposite of `SandboxRefused`. `sent` says whether the request may
    have reached the Controller: when it may, the call may have taken effect
    and nobody heard about it.
    """

    def __init__(self, detail: str, *, sent: bool) -> None:
        super().__init__(detail)
        self.sent = sent


class LeaseAuthority(Protocol):
    """Answers whether a Run currently holds a given lease.

    A port rather than a query, so the Controller does not reach into the Run
    module's tables — and so a Controller test is about the Controller's rules
    rather than about lease mechanics that phase 2B already proves.
    """

    async def holds(self, run_id: UUID, lease_id: UUID) -> bool: ...

    async def any_live(self, run_id: UUID) -> bool: ...


@dataclass(frozen=True)
class AuditEntry:
    action: str
    run_id: UUID
    sandbox_id: UUID
    detail: str


@dataclass(frozen=True)
class SweepResult:
    """What one host sweep removed."""

    containers: int
    volumes: int


class SandboxAudit(Protocol):
    async def record(self, entry: AuditEntry) -> None: ...


@dataclass(frozen=True)
class AcquireResult:
    sandbox_id: UUID
    cache_state: CacheState


@dataclass(frozen=True)
class WorkspaceTicket:
    """Proof that one workspace action was authorized, and for which container.

    The transport drives the engine with this in hand; the container id never
    comes from a payload, only from the reservation the checks walked.
    """

    container_id: str


#: Where every workspace byte lives inside the container. Fixed here, so no
#: payload can name another mount.
DATA_MOUNT = "/workspace/data"


class SandboxController:
    def __init__(
        self,
        *,
        engine: DockerEngine,
        store: SandboxStore,
        approved_digests: tuple[str, ...],
        leases: LeaseAuthority | None = None,
        audit: SandboxAudit | None = None,
        ceiling: ResourceProfile = DEFAULT_PROFILE,
        networking: SandboxNetworking | None = None,
        memory_budget_mb: int | None = None,
        memory_ceiling_mb: int | None = None,
        name: str = "default",
        commit: Callable[[], Awaitable[None]] | None = None,
        runtime: str = "runc",
    ) -> None:
        self.engine = engine
        self.store = store
        self.approved_digests = approved_digests
        self.leases: LeaseAuthority = leases or _AlwaysHolds()
        self.audit: SandboxAudit = audit or _Recording()
        self.ceiling = ceiling
        #: The sum of instance memory limits this controller admits (§21.3).
        #: None admits without counting, which only tests and tools want.
        self.memory_budget_mb = memory_budget_mb
        #: This sandbox host's name (tier C). Its admission, eviction and
        #: every call count only what is recorded under it.
        self.name = name
        #: Commits this call's transaction so far (§21.3, v2.24): an acquire
        #: commits its registration, and with it the admission lock, before it
        #: builds the container. None keeps the lock to the end of the call.
        self._commit = commit
        #: The OCI runtime every new sandbox runs on (§1.25, v2.25).
        self.runtime = runtime
        #: The largest sandbox an Agent may declare (§21.3, v2.22). `ceiling`
        #: is the default profile, so its memory is the size of an Agent that
        #: declares none; None here means that is also the largest.
        self.memory_ceiling_mb = (
            ceiling.memory_mb if memory_ceiling_mb is None else memory_ceiling_mb
        )
        # Absent on a deployment with no boundary, and then a sandbox has no
        # network at all — never an unguarded one.
        self.networking = networking

    # -- Worker actions ----------------------------------------------------

    async def acquire(
        self,
        *,
        run_id: UUID,
        lease_id: UUID,
        workspace_id: UUID,
        profile: str,
        session_id: UUID | None = None,
        memory_mb: int | None = None,
    ) -> AcquireResult:
        await self._require_lease(run_id, lease_id)
        # Locked: eviction skips a claim its own Run holds, so the container
        # cannot go between this read and the unpause.
        existing = await self.store.live_for_run_locked(run_id)
        if existing is not None:
            # Before thawing or discarding anything: a claim on another host
            # is a sandbox this host does not have, and making a second one
            # here is two sandboxes writing one workspace.
            await self._require_here(existing.instance_id)
            warm = await self._thaw_if_warm(existing)
            if warm is not None:
                return warm
            # §11.4: a keep past its deadline means "otherwise create new and
            # return reset". The Worker reclaims it here rather than waiting
            # for the Scheduler's scan — a Run should not be blocked because a
            # background sweep has not come round yet.
            await self._discard(existing)

        instance_id = uuid.uuid4()
        try:
            chosen = profile_named(profile, ceiling=self.ceiling)
        except ContainerPolicyError as refused:
            raise SandboxRefused(RefusalReason.IMAGE_NOT_APPROVED) from refused
        if memory_mb is not None:
            # Checked before anything is created, so a refused size leaves
            # nothing behind. Publishing already checked the ceiling; this is
            # what stands for a version published before it came down.
            if not MIN_SANDBOX_MEMORY_MB <= memory_mb <= self.memory_ceiling_mb:
                raise SandboxRefused(RefusalReason.MEMORY_OUT_OF_RANGE)
            chosen = replace(chosen, memory_mb=memory_mb)
        try:
            config = container_config(
                digest=self._digest(),
                profile=chosen,
                run_id=run_id,
                instance_id=instance_id,
                workspace_id=workspace_id,
                session_id=session_id,
                approved_digests=self.approved_digests,
                ceiling=replace(self.ceiling, memory_mb=self.memory_ceiling_mb),
                egress=None
                if self.networking is None
                else self.networking.attachment(instance_id),
                runtime=self.runtime,
            )
        except ContainerPolicyError as refused:
            # Before Docker is asked, so a refused image leaves nothing behind.
            raise SandboxRefused(RefusalReason.IMAGE_NOT_APPROVED) from refused

        # Counted and registered under the admission lock, then committed, so
        # the lock is not held while Docker builds the container (§21.3,
        # v2.24): a host builds its sandboxes at once, not in turn. The row is
        # `creating` and its memory counts from here, so two acquires still
        # cannot both take the last room.
        await self._admit(chosen.memory_mb)
        reservation = await self.store.reserve(
            run_id=run_id,
            workspace_id=workspace_id,
            instance=SandboxInstance(
                id=instance_id,
                container_id=None,
                image_digest=config.image,
                resource_profile=profile,
                boot_id=uuid.uuid4().hex,
                status=InstanceStatus.CREATING,
                memory_mb=chosen.memory_mb,
                controller=self.name,
            ),
        )
        if self._commit is not None:
            await self._commit()
        try:
            # Workspace design §7: a fresh instance restores into an empty
            # volume. Whatever this Run left on the host goes first — the
            # volume of an instance discarded or evicted, a container a crash
            # never recorded — so neither old files nor a stranger sharing the
            # mount survive.
            await self._clear_leftovers(run_id)
            # Explicitly, before the container, and with the full ownership
            # chain in labels: the Scheduler that later reclaims an orphan
            # enumerates by label (design §13), and it can only see what was
            # written.
            await self.engine.create_volume(_volume_name(run_id), config.volume_labels)
            if self.networking is not None:
                proxy_address = await self._open_network(
                    self.networking, instance_id, config.volume_labels
                )
                config = replace(
                    config, extra_hosts={self.networking.proxy_alias: proxy_address}
                )
            container_id = await self.engine.create(config)
        except Exception:
            # The registration was committed; left alone it would hold its
            # memory as a `creating` row for ever. Committed here because the
            # dispatch rolls back on anything but a refusal.
            await self._abandon(reservation, run_id)
            if self._commit is not None:
                await self._commit()
            raise
        await self.store.complete_instance(instance_id, container_id)
        await self._register_address(container_id, run_id, instance_id)
        return AcquireResult(sandbox_id=instance_id, cache_state=CacheState.RESET)

    async def _abandon(self, reservation: SandboxReservation, run_id: UUID) -> None:
        """Undo a build that failed: whatever got made, and the registration."""
        instance = await self.store.read_instance(reservation.instance_id)
        if instance is not None:
            await self._remove_container(instance)
            await self._close_network(instance.id)
            await self.store.set_instance_status(instance.id, InstanceStatus.DESTROYED)
        await self.engine.remove_volume_if_present(_volume_name(run_id))
        await self.store.release(reservation.id)

    async def execute(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID, command: SandboxCommand
    ) -> CommandResult:
        await self._require_lease(run_id, lease_id)
        instance = await self._require_running(run_id, sandbox_id)
        if not _inside_workspace(command.cwd):
            raise SandboxRefused(RefusalReason.WORKING_DIRECTORY_NOT_ALLOWED)
        return await self.engine.execute(_container(instance), command)

    # -- Workspace actions ---------------------------------------------------

    async def workspace_import(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID, declared_total: int
    ) -> WorkspaceTicket:
        """Authorize a restore into a frozen, clean instance.

        Freezing first is design §7's order: the first import byte may only
        land in a container no process can be changing.
        """
        await self._require_lease(run_id, lease_id)
        instance = await self._require_frozen(run_id, sandbox_id)
        if declared_total < 0:
            raise SandboxRefused(RefusalReason.WORKING_DIRECTORY_NOT_ALLOWED)
        return WorkspaceTicket(container_id=_container(instance))

    async def workspace_import_failed(self, *, run_id: UUID, sandbox_id: UUID) -> None:
        """An interrupted import leaves a tree nobody can vouch for.

        The instance is marked dirty so only destruction remains; the model
        never sees a partially restored tree (design §7).
        """
        reservation = await self._reservation(run_id, sandbox_id)
        await self.store.set_instance_status(sandbox_id, InstanceStatus.ISOLATED)
        await self.store.isolate(reservation.id, reason="import_interrupted")
        await self.audit.record(
            AuditEntry(
                action="sandbox.import_failed",
                run_id=run_id,
                sandbox_id=sandbox_id,
                detail="instance dirtied by an interrupted import",
            )
        )

    async def workspace_scan(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID
    ) -> tuple[ScannedEntry, ...]:
        await self._require_lease(run_id, lease_id)
        instance = await self._require_frozen(run_id, sandbox_id)
        return await self.engine.scan_tree(_container(instance), DATA_MOUNT)

    async def workspace_export(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID
    ) -> WorkspaceTicket:
        await self._require_lease(run_id, lease_id)
        instance = await self._require_frozen(run_id, sandbox_id)
        return WorkspaceTicket(container_id=_container(instance))

    async def execute_stream(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID, command: SandboxCommand
    ) -> WorkspaceTicket:
        """Authorize a streamed command: the checks `execute` makes, no more."""
        await self._require_lease(run_id, lease_id)
        instance = await self._require_running(run_id, sandbox_id)
        if not _inside_workspace(command.cwd):
            raise SandboxRefused(RefusalReason.WORKING_DIRECTORY_NOT_ALLOWED)
        return WorkspaceTicket(container_id=_container(instance))

    async def volume_remove(self, *, run_id: UUID, sandbox_id: UUID) -> None:
        """Scheduler authority: reclaim a data volume nothing live still owns.

        Refused while a lease is live or while an active claim still believes
        it is executing; an isolated claim is exactly the case this exists
        for. The volume name is derived from the server-side Run id — no
        payload names a volume.
        """
        if await self.leases.any_live(run_id):
            raise SandboxRefused(RefusalReason.LEASE_STILL_LIVE)
        claim = await self.store.live_for_run(run_id)
        if claim is not None:
            if claim.instance_id != sandbox_id:
                raise SandboxRefused(RefusalReason.RESERVATION_NOT_OWNED)
            await self._require_here(claim.instance_id)
            if claim.status is not ReservationStatus.ISOLATED:
                raise SandboxRefused(RefusalReason.RESERVATION_STILL_LIVE)
        # Already gone is what was wanted, as for the container (2026-10-24).
        await self.engine.remove_volume_if_present(_volume_name(run_id))
        await self.audit.record(
            AuditEntry(
                action="sandbox.volume_remove",
                run_id=run_id,
                sandbox_id=sandbox_id,
                detail="data volume reclaimed",
            )
        )

    async def freeze(self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID) -> None:
        await self._require_lease(run_id, lease_id)
        instance = await self._require_running(run_id, sandbox_id)
        await self.engine.pause(_container(instance))
        # Before the status, so there is no window in which a paused container
        # is still somebody as far as the proxy is concerned.
        await self.store.clear_egress_address(sandbox_id)
        await self.store.set_instance_status(sandbox_id, InstanceStatus.FROZEN)

    async def thaw(self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID) -> None:
        await self._require_lease(run_id, lease_id)
        instance = await self._require_owned(run_id, sandbox_id)
        await self.engine.unpause(_container(instance))
        await self.store.set_instance_status(sandbox_id, InstanceStatus.RUNNING)
        await self._register_address(_container(instance), run_id, sandbox_id)

    async def destroy(self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID) -> None:
        await self._require_lease(run_id, lease_id)
        await self._remove(run_id, sandbox_id)

    async def keep(self, *, run_id: UUID, sandbox_id: UUID, until: datetime) -> None:
        """Hold a frozen instance warm for this Run's next slice."""
        reservation = await self._reservation(run_id, sandbox_id)
        await self.store.keep(reservation.id, idle_expires_at=until)

    async def inspect(self, *, run_id: UUID, sandbox_id: UUID) -> SandboxInstance:
        return await self._require_owned(run_id, sandbox_id)

    # -- Scheduler action --------------------------------------------------

    async def sweep(self) -> SweepResult:
        """Remove from this host what a crash left and nothing records (§13).

        A container is kept only while a live instance row names it. Held
        under the admission lock, which every acquire takes before it starts a
        container and keeps until the row commits: so a container without a
        row here is one whose acquire died, never one still on its way.

        A volume is kept while its Run holds a claim or a lease. Without
        either, nothing mounts it again: a Run that comes back restores into
        a new one.
        """
        await self.store.lock_admission(self.name)
        containers = 0
        for found in await self.engine.containers_labelled(_RUN_LABEL):
            if await self._recorded(found):
                continue
            await self.engine.remove_if_present(found.id)
            containers += 1
            await self._audit_sweep(found.labels, "container nothing recorded removed")
        if self.networking is not None:
            for network in await self.engine.networks_labelled(_RUN_LABEL):
                if await self._network_recorded(network.labels):
                    await self._keep_proxy_on(self.networking, network.name)
                    continue
                if await self.engine.remove_network_if_present(network.name):
                    await self._audit_sweep(network.labels, "network nothing recorded removed")
        volumes = 0
        for volume in await self.engine.volumes_labelled(_RUN_LABEL):
            run_id = _label_uuid(volume.labels, _RUN_LABEL)
            if run_id is None or volume.name != _volume_name(run_id):
                continue
            if await self.store.live_for_run(run_id) is not None:
                continue
            if await self.leases.any_live(run_id):
                continue
            try:
                removed = await self.engine.remove_volume_if_present(volume.name)
            except DockerUnavailable:
                # Still mounted, by a container this pass could not remove.
                logger.warning("sandbox volume left in place", extra={"volume": volume.name})
                continue
            if removed:
                volumes += 1
                await self._audit_sweep(volume.labels, "data volume nothing owns removed")
        return SweepResult(containers=containers, volumes=volumes)

    async def cleanup(self, *, run_id: UUID, sandbox_id: UUID) -> None:
        """Reclaim a sandbox whose Run no longer holds a lease.

        Refused while a lease is live: the Scheduler's authority exists for
        after expiry, and using it earlier would destroy the container of a Run
        that is at that moment executing in it.
        """
        if await self.leases.any_live(run_id):
            raise SandboxRefused(RefusalReason.LEASE_STILL_LIVE)
        await self._remove(run_id, sandbox_id)
        await self.audit.record(
            AuditEntry(
                action="sandbox.cleanup",
                run_id=run_id,
                sandbox_id=sandbox_id,
                detail="reclaimed after lease expiry",
            )
        )

    # -- Internals ---------------------------------------------------------

    async def _thaw_if_warm(self, existing: SandboxReservation) -> AcquireResult | None:
        """This Run's own frozen instance, if the TTL has not run out.

        `active` and `isolated` are refusals rather than thaws: active means
        another slice believes it is running in there, and isolated means the
        platform is not sure the container is gone. Handing either one back
        would be the leak the isolated state exists to prevent.
        """
        if existing.status is not ReservationStatus.KEPT:
            raise SandboxRefused(RefusalReason.ALREADY_RESERVED)
        if existing.idle_expires_at is None or existing.idle_expires_at <= datetime.now(UTC):
            return None
        instance = await self.store.read_instance(existing.instance_id)
        if instance is None:
            return None
        if not await self.engine.unpause_if_present(_container(instance)):
            # The row outlived its container: an eviction rolled back after
            # the container went, or somebody removed it by hand. Discarded
            # like an expired keep, and the Run gets a new instance.
            return None
        await self.store.set_instance_status(instance.id, InstanceStatus.RUNNING)
        # The freeze that ended the last slice took the address away, and the
        # Worker does not thaw a reused instance: this is its thaw.
        await self._register_address(_container(instance), existing.run_id, instance.id)
        return AcquireResult(sandbox_id=instance.id, cache_state=CacheState.REUSED)

    async def _admit(self, needed_mb: int) -> None:
        """Make room for `needed_mb` within the budget, or refuse (§21.3).

        Counted by limits, not by use: an instance using little now may grow
        to its limit later. The lock is held until this call's transaction
        commits, so the container created after this returns is in the count
        before any other acquire takes its own.
        """
        # Taken even without a budget: the host sweep relies on every acquire
        # holding it from before `docker run` until its row commits.
        await self.store.lock_admission(self.name)
        budget = self.memory_budget_mb
        if budget is None:
            return
        if needed_mb > budget:
            # Never fits, however much is evicted: refusing it as exhausted
            # would requeue the Run to be refused every ten seconds, forever.
            raise SandboxRefused(RefusalReason.MEMORY_OUT_OF_RANGE)
        shortfall = await self.store.committed_memory_mb(self.name) + needed_mb - budget
        if shortfall <= 0:
            return
        chosen: list[tuple[SandboxReservation, SandboxInstance]] = []
        freed = 0
        for reservation in await self.store.evictable_keeps(self.name):
            if freed >= shortfall:
                break
            # A frozen instance under a kept claim may still belong to a Run
            # that is executing: every checkpoint freezes the container, and a
            # Run thawed at a slice boundary keeps its kept claim. Only a Run
            # between slices has no live lease. On 2026-09-30 skipping this
            # check evicted a sandbox mid-round and interrupted its Run.
            if await self.leases.any_live(reservation.run_id):
                continue
            instance = await self.store.read_instance(reservation.instance_id)
            if instance is None:
                continue
            chosen.append((reservation, instance))
            freed += instance.memory_mb
        if freed < shortfall:
            # Nothing is evicted: a warm sandbox destroyed without making
            # enough room is a cold start for its Run and no gain for this one.
            SANDBOX_ADMISSION_REJECTED.inc()
            raise SandboxRefused(RefusalReason.MEMORY_BUDGET_EXHAUSTED)
        for reservation, instance in chosen:
            await self._evict(reservation, instance)

    async def _evict(self, reservation: SandboxReservation, instance: SandboxInstance) -> None:
        """A frozen instance goes the way an expired keep does, early.

        Its Run's next slice starts a new instance from the committed
        workspace, as after the idle TTL. The volume stays, as in `_discard`,
        until that acquire clears it or the host sweep finds it unowned.
        """
        await self._remove_container(instance)
        await self._close_network(instance.id)
        await self.store.set_instance_status(instance.id, InstanceStatus.DESTROYED)
        await self.store.release(reservation.id)
        await self.audit.record(
            AuditEntry(
                action="sandbox.evict",
                run_id=reservation.run_id,
                sandbox_id=instance.id,
                detail="evicted to fit the sandbox memory budget",
            )
        )

    async def _discard(self, reservation: SandboxReservation) -> None:
        instance = await self.store.read_instance(reservation.instance_id)
        if instance is not None:
            await self._remove_container(instance)
            await self._close_network(instance.id)
            await self.store.set_instance_status(instance.id, InstanceStatus.DESTROYED)
        await self.store.release(reservation.id)

    async def _clear_leftovers(self, run_id: UUID) -> None:
        for found in await self.engine.containers_labelled(_RUN_LABEL, str(run_id)):
            await self.engine.remove_if_present(found.id)
            await self._audit_sweep(found.labels, "container its Run left unrecorded removed")
        if self.networking is not None:
            for network in await self.engine.networks_labelled(_RUN_LABEL, str(run_id)):
                await self.engine.remove_network_if_present(network.name)
        await self.engine.remove_volume_if_present(_volume_name(run_id))

    async def _open_network(
        self, networking: SandboxNetworking, instance_id: UUID, labels: dict[str, str]
    ) -> str:
        """This sandbox's bridge, on a /28 from the pool, with the proxy on it.

        Random rather than counted: Docker already knows which subnets are in
        use and says so when one overlaps, and a table of our own would be a
        second record of the same fact, free to disagree with the first.

        Returns the proxy's address there, which is fixed — the subnet's
        second, after the gateway — so the sandbox's hosts entry names an
        address Docker did not pick at random.
        """
        name = networking.network_name(instance_id)
        subnets = list(networking.pool.subnets(new_prefix=_SANDBOX_PREFIX))
        for subnet in random.sample(subnets, min(_SUBNET_ATTEMPTS, len(subnets))):
            try:
                await self.engine.create_network(name, str(subnet), labels)
            except SubnetTaken:
                continue
            address = str(subnet[2])
            await self.engine.connect(
                name, networking.proxy_container, [networking.proxy_alias], address=address
            )
            return address
        raise SandboxRefused(RefusalReason.NETWORK_POOL_EXHAUSTED)

    async def _keep_proxy_on(self, networking: SandboxNetworking, name: str) -> None:
        """Put the proxy back on a live sandbox's network if it is not there.

        A recreated proxy container comes back on its own networks only, and
        nothing else joins it to the sandbox networks again: every sandbox
        alive across the change had no way out until it was rebuilt. Back at
        the fixed address, which is what the sandbox's hosts entry names.
        """
        subnet, attached = await self.engine.network_endpoints(name)
        if subnet is None or networking.proxy_container in attached:
            return
        await self.engine.connect(
            name,
            networking.proxy_container,
            [networking.proxy_alias],
            address=str(IPv4Network(subnet)[2]),
        )
        logger.warning("egress proxy put back on a sandbox network", extra={"network": name})

    async def _close_network(self, instance_id: UUID) -> None:
        """After the container: Docker will not remove a network in use."""
        if self.networking is not None:
            await self.engine.remove_network_if_present(self.networking.network_name(instance_id))

    async def _network_recorded(self, labels: dict[str, str]) -> bool:
        instance_id = _label_uuid(labels, _INSTANCE_LABEL)
        if instance_id is None:
            return False
        instance = await self.store.read_instance(instance_id)
        return instance is not None and instance.status is not InstanceStatus.DESTROYED

    async def _remove_container(self, instance: SandboxInstance) -> None:
        """Remove an instance's container, by id or, before one was written, by label.

        An instance left `creating` by a crash mid-build may have a container
        nobody wrote down; it still carries the instance label it was made
        with. A container that is already gone is what was wanted: on
        2026-10-24 one deleted outside the platform made every cleanup of its
        Run fail on Docker's 404, and the Run stayed interrupted for good.
        Any other failure still raises.
        """
        if instance.container_id is not None:
            await self.engine.remove_if_present(instance.container_id)
            return
        for found in await self.engine.containers_labelled(_INSTANCE_LABEL, str(instance.id)):
            await self.engine.remove_if_present(found.id)

    async def _recorded(self, found: ContainerInfo) -> bool:
        instance_id = _label_uuid(found.labels, _INSTANCE_LABEL)
        if instance_id is None:
            return False
        instance = await self.store.read_instance(instance_id)
        if instance is None or instance.status is InstanceStatus.DESTROYED:
            return False
        # Registered before it is built (v2.24): a `creating` row with no
        # container id yet is an acquire on its way, not one that died.
        return instance.status is InstanceStatus.CREATING or instance.container_id == found.id

    async def _audit_sweep(self, labels: dict[str, str], detail: str) -> None:
        run_id = _label_uuid(labels, _RUN_LABEL)
        sandbox_id = _label_uuid(labels, _INSTANCE_LABEL)
        if run_id is None or sandbox_id is None:
            return
        await self.audit.record(
            AuditEntry(action="sandbox.sweep", run_id=run_id, sandbox_id=sandbox_id, detail=detail)
        )

    async def _register_address(
        self, container_id: str, run_id: UUID, sandbox_id: UUID
    ) -> None:
        """Write down where this container's packets will come from.

        A container with no network has no address and needs no row: it cannot
        reach the proxy, so nobody will ever ask who it is.
        """
        address = await self.engine.address_of(container_id)
        if address is None:
            return
        await self.store.register_egress_address(
            address=address, run_id=run_id, sandbox_id=sandbox_id
        )

    async def _remove(self, run_id: UUID, sandbox_id: UUID) -> None:
        reservation, instance = await self._claim(run_id, sandbox_id)
        # Before the container goes: once Docker has the address back it may
        # hand it to the next container, and a stale row would make that
        # container this Run.
        await self.store.clear_egress_address(sandbox_id)
        if instance is not None:
            await self._remove_container(instance)
            await self._close_network(sandbox_id)
            await self.store.set_instance_status(sandbox_id, InstanceStatus.DESTROYED)
        await self.store.release(reservation.id)
        # Container first, then volume: the volume cannot be in use once the
        # container is confirmed gone. Session state lives in object-store revisions;
        # a Run's teardown owes the host nothing but this reclamation.
        # Already gone is what was wanted, as for the container (2026-10-24).
        await self.engine.remove_volume_if_present(_volume_name(run_id))
        await self.audit.record(
            AuditEntry(
                action="sandbox.volume_remove",
                run_id=run_id,
                sandbox_id=sandbox_id,
                detail="data volume removed with its sandbox",
            )
        )

    async def _require_lease(self, run_id: UUID, lease_id: UUID) -> None:
        if not await self.leases.holds(run_id, lease_id):
            raise SandboxRefused(RefusalReason.LEASE_INVALID)

    async def _claim(
        self, run_id: UUID, sandbox_id: UUID
    ) -> tuple[SandboxReservation, SandboxInstance | None]:
        """The Run's live claim on this sandbox, and the instance, read once.

        Every check a call makes is answered from this one read: whose claim
        it is, and which host the sandbox is on. Each call used to read the
        instance two or three times (2026-10-24), about five calls a round.
        """
        found = await self.store.live_for_run(run_id)
        if found is None:
            raise SandboxRefused(RefusalReason.NO_RESERVATION)
        if found.instance_id != sandbox_id:
            # The `sandbox_id` is the caller's input, so it is checked rather
            # than trusted.
            raise SandboxRefused(RefusalReason.RESERVATION_NOT_OWNED)
        instance = await self.store.read_instance(sandbox_id)
        self._refuse_elsewhere(instance)
        return found, instance

    async def _reservation(self, run_id: UUID, sandbox_id: UUID) -> SandboxReservation:
        reservation, _ = await self._claim(run_id, sandbox_id)
        return reservation

    async def _require_here(self, sandbox_id: UUID) -> None:
        self._refuse_elsewhere(await self.store.read_instance(sandbox_id))

    def _refuse_elsewhere(self, instance: SandboxInstance | None) -> None:
        """Refuse a sandbox recorded on another host (tier C).

        Routing sends every call to the sandbox's own host, so this is what
        stands when one arrives elsewhere anyway: acting on it here would be
        acting on a container this host does not have, or making a second.
        """
        if instance is not None and instance.controller != self.name:
            raise SandboxRefused(RefusalReason.RESERVATION_ELSEWHERE)

    async def _require_owned(self, run_id: UUID, sandbox_id: UUID) -> SandboxInstance:
        _, instance = await self._claim(run_id, sandbox_id)
        if instance is None:
            raise SandboxRefused(RefusalReason.NO_RESERVATION)
        return instance

    async def _require_running(self, run_id: UUID, sandbox_id: UUID) -> SandboxInstance:
        instance = await self._require_owned(run_id, sandbox_id)
        if instance.status is not InstanceStatus.RUNNING:
            # Executing in a frozen container would either hang or silently
            # thaw it, and both are worse than saying no.
            raise SandboxRefused(RefusalReason.NOT_RUNNING)
        return instance

    async def _require_frozen(self, run_id: UUID, sandbox_id: UUID) -> SandboxInstance:
        instance = await self._require_owned(run_id, sandbox_id)
        if instance.status is InstanceStatus.ISOLATED:
            # Dirtied by an interrupted import; only destruction remains.
            raise SandboxRefused(RefusalReason.INSTANCE_DIRTY)
        if instance.status is not InstanceStatus.FROZEN:
            # Scanning or importing while processes run would checkpoint files
            # that are still being changed (design §7/§8).
            raise SandboxRefused(RefusalReason.NOT_FROZEN)
        return instance

    def _digest(self) -> str:
        return self.approved_digests[0] if self.approved_digests else ""


#: Two addresses are used, the sandbox's and the proxy's; a /28 leaves room
#: and a /16 pool holds 4096 of them.
_SANDBOX_PREFIX = 28
_SUBNET_ATTEMPTS = 8
_RUN_LABEL = "tiny-hermes.run"
_INSTANCE_LABEL = "tiny-hermes.instance"


def _label_uuid(labels: dict[str, str], key: str) -> UUID | None:
    try:
        return UUID(labels[key])
    except (KeyError, ValueError):
        return None


def _volume_name(run_id: UUID) -> str:
    """Server-derived, always: no payload may name a volume (design §13)."""
    return f"tiny-hermes-data-{run_id}"


def _inside_workspace(cwd: str) -> bool:
    """Compared after normalizing, so `..` cannot walk out of an allowed root."""
    parts: list[str] = []
    for piece in cwd.split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            if parts:
                parts.pop()
            continue
        parts.append(piece)
    resolved = "/" + "/".join(parts)
    return any(
        resolved == allowed or resolved.startswith(f"{allowed}/")
        for allowed in ALLOWED_WORKING_DIRECTORIES
    )


class _AlwaysHolds:
    """The default authority, for a deployment that has not wired one yet.

    Permissive on purpose and only reachable in tests: the real one is passed in
    by the process that has a database. A Controller constructed without one in
    production would be a wiring bug, not a security decision, and the process
    ban plus the Compose boundary are what stop that mattering.
    """

    async def holds(self, run_id: UUID, lease_id: UUID) -> bool:
        del run_id, lease_id
        return True

    async def any_live(self, run_id: UUID) -> bool:
        del run_id
        return False


class _Recording:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


def _container(instance: SandboxInstance) -> str:
    """The container of an instance an operation needs built.

    Only a `creating` instance has none, and nothing but its own acquire may
    act on it before the build finishes.
    """
    if instance.container_id is None:
        raise SandboxRefused(RefusalReason.NOT_RUNNING)
    return instance.container_id
