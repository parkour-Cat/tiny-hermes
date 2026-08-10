import asyncio
from uuid import UUID


class NullWakeUpNotifier:
    """Carries no notifications at all.

    Used when no Redis URL is configured and by tests that must prove the
    platform still finds work through the database alone.
    """

    async def publish(self, workspace_id: UUID, run_id: UUID) -> None:
        del workspace_id, run_id

    async def wait(self, timeout_seconds: float) -> bool:
        await asyncio.sleep(timeout_seconds)
        return False

    async def close(self) -> None:
        return None
