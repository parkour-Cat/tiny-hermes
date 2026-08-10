from typing import Protocol
from uuid import UUID


class WakeUpNotifier(Protocol):
    """Latency-only notification of newly claimable work.

    A notifier is never consulted to decide what is true. Losing every message
    it carries must cost nothing but the next poll interval.
    """

    async def publish(self, workspace_id: UUID, run_id: UUID) -> None: ...

    async def wait(self, timeout_seconds: float) -> bool: ...

    async def close(self) -> None: ...
