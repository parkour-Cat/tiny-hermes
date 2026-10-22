"""The controller's gauges (observability design §3.4), read from its rows.

From the rows rather than counted as actions happen: a controller that
restarts would otherwise publish zero sandboxes while dozens are still alive.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from tiny_hermes.sandbox.domain.models import InstanceStatus
from tiny_hermes.sandbox.infrastructure.sql_store import SqlSandboxStore
from tiny_hermes.shared.metrics import (
    SANDBOX_MEMORY_BUDGET,
    SANDBOX_MEMORY_RESERVED,
    SANDBOXES,
)

logger = logging.getLogger(__name__)

_MiB = 1024 * 1024


async def publish_sandbox_gauges(store: SqlSandboxStore, *, budget_mb: int) -> None:
    by_status = await store.live_instances_by_status()
    for status in InstanceStatus:
        SANDBOXES.labels(state=status.value).set(by_status.get(status.value, 0))
    SANDBOX_MEMORY_RESERVED.set(await store.committed_memory_mb() * _MiB)
    SANDBOX_MEMORY_BUDGET.set(budget_mb * _MiB)


async def observe_forever(publish: Callable[[], Awaitable[None]], *, every: float) -> None:
    while True:
        try:
            await publish()
        except Exception:
            logger.exception("sandbox observation failed")
        await asyncio.sleep(every)
