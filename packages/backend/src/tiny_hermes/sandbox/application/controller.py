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

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
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
    ContainerPolicyError,
    EgressNetwork,
    ResourceProfile,
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
from tiny_hermes.sandbox.infrastructure.docker_engine import DockerEngine
from tiny_hermes.sandbox.ports.store import SandboxStore


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
    WORKING_DIRECTORY_NOT_ALLOWED = "working_directory_not_allowed"


class SandboxRefused(Exception):
    def __init__(self, reason: RefusalReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


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
        egress: EgressNetwork | None = None,
    ) -> None:
        self.engine = engine
        self.store = store
        self.approved_digests = approved_digests
        self.leases: LeaseAuthority = leases or _AlwaysHolds()
        self.audit: SandboxAudit = audit or _Recording()
        self.ceiling = ceiling
        # Absent on a deployment with no boundary, and then a sandbox has no
        # network at all — never an unguarded one.
        self.egress = egress

    # -- Worker actions ----------------------------------------------------

    async def acquire(
        self,
        *,
        run_id: UUID,
        lease_id: UUID,
        workspace_id: UUID,
        profile: str,
        session_id: UUID | None = None,
    ) -> AcquireResult:
        await self._require_lease(run_id, lease_id)
        existing = await self.store.live_for_run(run_id)
        if existing is not None:
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
            config = container_config(
                digest=self._digest(),
                profile=profile_named(profile, ceiling=self.ceiling),
                run_id=run_id,
                instance_id=instance_id,
                workspace_id=workspace_id,
                session_id=session_id,
                approved_digests=self.approved_digests,
                ceiling=self.ceiling,
                egress=self.egress,
            )
        except ContainerPolicyError as refused:
            # Before Docker is asked, so a refused image leaves nothing behind.
            raise SandboxRefused(RefusalReason.IMAGE_NOT_APPROVED) from refused

        # Explicitly, before the container, and with the full ownership chain
        # in labels: the Scheduler that later reclaims an orphan enumerates by
        # label (design §13), and it can only see what was written.
        await self.engine.create_volume(_volume_name(run_id), config.volume_labels)
        container_id = await self.engine.create(config)
        await self.store.reserve(
            run_id=run_id,
            workspace_id=workspace_id,
            instance=SandboxInstance(
                id=instance_id,
                container_id=container_id,
                image_digest=config.image,
                resource_profile=profile,
                boot_id=uuid.uuid4().hex,
                status=InstanceStatus.RUNNING,
            ),
        )
        await self._register_address(container_id, run_id, instance_id)
        return AcquireResult(sandbox_id=instance_id, cache_state=CacheState.RESET)

    async def execute(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID, command: SandboxCommand
    ) -> CommandResult:
        await self._require_lease(run_id, lease_id)
        instance = await self._require_running(run_id, sandbox_id)
        if not _inside_workspace(command.cwd):
            raise SandboxRefused(RefusalReason.WORKING_DIRECTORY_NOT_ALLOWED)
        return await self.engine.execute(instance.container_id, command)

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
        return WorkspaceTicket(container_id=instance.container_id)

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
        return await self.engine.scan_tree(instance.container_id, DATA_MOUNT)

    async def workspace_export(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID
    ) -> WorkspaceTicket:
        await self._require_lease(run_id, lease_id)
        instance = await self._require_frozen(run_id, sandbox_id)
        return WorkspaceTicket(container_id=instance.container_id)

    async def execute_stream(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID, command: SandboxCommand
    ) -> WorkspaceTicket:
        """Authorize a streamed command: the checks `execute` makes, no more."""
        await self._require_lease(run_id, lease_id)
        instance = await self._require_running(run_id, sandbox_id)
        if not _inside_workspace(command.cwd):
            raise SandboxRefused(RefusalReason.WORKING_DIRECTORY_NOT_ALLOWED)
        return WorkspaceTicket(container_id=instance.container_id)

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
            if claim.status is not ReservationStatus.ISOLATED:
                raise SandboxRefused(RefusalReason.RESERVATION_STILL_LIVE)
        await self.engine.remove_volume(_volume_name(run_id))
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
        await self.engine.pause(instance.container_id)
        # Before the status, so there is no window in which a paused container
        # is still somebody as far as the proxy is concerned.
        await self.store.clear_egress_address(sandbox_id)
        await self.store.set_instance_status(sandbox_id, InstanceStatus.FROZEN)

    async def thaw(self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID) -> None:
        await self._require_lease(run_id, lease_id)
        instance = await self._require_owned(run_id, sandbox_id)
        await self.engine.unpause(instance.container_id)
        await self.store.set_instance_status(sandbox_id, InstanceStatus.RUNNING)
        await self._register_address(instance.container_id, run_id, sandbox_id)

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
        await self.engine.unpause(instance.container_id)
        await self.store.set_instance_status(instance.id, InstanceStatus.RUNNING)
        return AcquireResult(sandbox_id=instance.id, cache_state=CacheState.REUSED)

    async def _discard(self, reservation: SandboxReservation) -> None:
        instance = await self.store.read_instance(reservation.instance_id)
        if instance is not None:
            await self.engine.remove(instance.container_id)
            await self.store.set_instance_status(instance.id, InstanceStatus.DESTROYED)
        await self.store.release(reservation.id)

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
        reservation = await self._reservation(run_id, sandbox_id)
        instance = await self.store.read_instance(sandbox_id)
        # Before the container goes: once Docker has the address back it may
        # hand it to the next container, and a stale row would make that
        # container this Run.
        await self.store.clear_egress_address(sandbox_id)
        if instance is not None:
            await self.engine.remove(instance.container_id)
            await self.store.set_instance_status(sandbox_id, InstanceStatus.DESTROYED)
        await self.store.release(reservation.id)
        # Container first, then volume: the volume cannot be in use once the
        # container is confirmed gone. Session state lives in MinIO revisions;
        # a Run's teardown owes the host nothing but this reclamation.
        await self.engine.remove_volume(_volume_name(run_id))
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

    async def _reservation(self, run_id: UUID, sandbox_id: UUID) -> SandboxReservation:
        found = await self.store.live_for_run(run_id)
        if found is None:
            raise SandboxRefused(RefusalReason.NO_RESERVATION)
        if found.instance_id != sandbox_id:
            # The `sandbox_id` is the caller's input, so it is checked rather
            # than trusted.
            raise SandboxRefused(RefusalReason.RESERVATION_NOT_OWNED)
        return found

    async def _require_owned(self, run_id: UUID, sandbox_id: UUID) -> SandboxInstance:
        await self._reservation(run_id, sandbox_id)
        instance = await self.store.read_instance(sandbox_id)
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
