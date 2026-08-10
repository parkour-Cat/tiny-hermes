import asyncio
import contextlib
import json
import logging
from time import monotonic
from typing import Any, cast
from uuid import UUID

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

CHANNEL = "tiny-hermes:run-wakeup"


class RedisWakeUpNotifier:
    """Publishes and receives claimable-work notifications over Redis.

    Every failure is swallowed. The business transaction that triggered a
    publish has already committed, and a lost notification costs one poll
    interval, so an unreachable Redis must never turn into a request error or
    a stalled Worker.
    """

    def __init__(self, url: str, channel: str = CHANNEL) -> None:
        self._url = url
        self._channel = channel
        self._client: Redis | None = None
        self._subscription: Any = None
        self._received: list[dict[str, str]] = []

    async def publish(self, workspace_id: UUID, run_id: UUID) -> None:
        payload = json.dumps(
            {"workspace_id": str(workspace_id), "run_id": str(run_id)},
            separators=(",", ":"),
        )
        try:
            await self._connect().publish(  # pyright: ignore[reportUnknownMemberType]
                self._channel, payload
            )
        except Exception:
            logger.warning("wake-up publish failed; workers will poll", exc_info=True)

    async def wait(self, timeout_seconds: float) -> bool:
        """Return True when a notification arrived before the timeout.

        The read loops until the deadline because a single ``get_message`` can
        be spent on a subscribe confirmation and report nothing.
        """
        deadline = monotonic() + timeout_seconds
        try:
            subscription = await self._subscribe()
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                message = await subscription.get_message(
                    ignore_subscribe_messages=True, timeout=min(remaining, 0.25)
                )
                if message is not None:
                    self._remember(message)
                    return True
        except Exception:
            logger.warning("wake-up wait failed; falling back to polling", exc_info=True)
            await asyncio.sleep(max(min(deadline - monotonic(), 1.0), 0))
            return False

    def drain(self) -> list[dict[str, str]]:
        """Take the notifications received so far. Diagnostics and tests only."""
        received, self._received = self._received, []
        return received

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            if self._subscription is not None:
                await self._subscription.aclose()
        with contextlib.suppress(Exception):
            if self._client is not None:
                await self._client.aclose()
        self._subscription = None
        self._client = None

    def _connect(self) -> Redis:
        if self._client is None:
            self._client = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
                self._url, socket_connect_timeout=1, socket_timeout=2
            )
        return self._client

    async def _subscribe(self) -> Any:
        if self._subscription is None:
            subscription = self._connect().pubsub()  # pyright: ignore[reportUnknownMemberType]
            await subscription.subscribe(self._channel)
            self._subscription = subscription
        return self._subscription

    def _remember(self, message: dict[str, Any]) -> None:
        data: Any = message.get("data")
        if not isinstance(data, bytes | str):
            return
        with contextlib.suppress(ValueError):
            decoded: Any = json.loads(data)
            if isinstance(decoded, dict):
                fields = cast(dict[Any, Any], decoded)
                self._received.append(
                    {str(key): str(fields[key]) for key in fields}
                )
