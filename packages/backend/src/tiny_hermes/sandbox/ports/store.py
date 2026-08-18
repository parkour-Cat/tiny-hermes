from datetime import datetime
from typing import Protocol
from uuid import UUID

from tiny_hermes.sandbox.domain.models import (
    InstanceStatus,
    SandboxInstance,
    SandboxReservation,
)


class SandboxStore(Protocol):
    async def reserve(
        self, *, run_id: UUID, workspace_id: UUID, instance: SandboxInstance
    ) -> SandboxReservation:
        """Claim a container for a Run.

        Raises `IntegrityError` when the Run already holds a live claim. The
        caller does not check first: a check followed by a write is a race two
        Workers can both win.
        """
        ...

    async def live_for_run(self, run_id: UUID) -> SandboxReservation | None: ...

    async def read(self, reservation_id: UUID) -> SandboxReservation | None: ...

    async def keep(
        self, reservation_id: UUID, *, idle_expires_at: datetime
    ) -> SandboxReservation: ...

    async def isolate(self, reservation_id: UUID, *, reason: str) -> SandboxReservation: ...

    async def release(self, reservation_id: UUID) -> SandboxReservation: ...

    async def expired_keeps(self, now: datetime) -> list[SandboxReservation]: ...

    async def isolated(self, limit: int) -> list[SandboxReservation]: ...

    async def read_instance(self, instance_id: UUID) -> SandboxInstance | None: ...

    async def register_egress_address(
        self, *, address: str, run_id: UUID, sandbox_id: UUID
    ) -> None:
        """Record which Run a sandbox address belongs to, while it holds one.

        The proxy answers "who is this" from this row, because a sandbox
        presents nothing. Writing it is the Controller's job: it is the only
        thing that knows the address Docker handed out.
        """
        ...

    async def clear_egress_address(self, sandbox_id: UUID) -> None:
        """Take the identity away — on freeze, and on destroy.

        §16.4: a frozen instance may not open a new connection. And a destroyed
        one must not lend its identity to whatever Docker gives the address to
        next, which is the case that would be a real hole rather than an
        inconvenience.
        """
        ...

    async def set_instance_status(
        self, instance_id: UUID, status: InstanceStatus
    ) -> SandboxInstance: ...
