"""Telling the API's event hub that a Run has news (capacity plan item 5).

Latency only, like the wake-up channel: a subscriber still reads its events
from the database, and a lost announcement costs one of the hub's fallback
intervals, never an event.

The write side hangs off the session rather than off each caller. Events are
appended by the Worker, the Scheduler and a dozen API paths, each committing
in its own place; ``SqlRunStore.append_events`` is the one seam they share,
and the commit is the one moment a reader in another process can see them.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from time import monotonic
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

CHANNEL = "tiny-hermes:run-events"
#: ``Session.info`` key of the callable told which Runs a commit made visible.
ANNOUNCE = "tiny_hermes.run_events.announce"
_APPENDED = "tiny_hermes.run_events.appended"

#: A listener that hears nothing, not even its own ping, for longer than
#: twice this is treated as cut off. A half-open TCP link raises nothing.
PING_SECONDS = 5.0
RECONNECT_SECONDS = 1.0
#: Publishes still waiting on a slow Redis beyond this are dropped rather
#: than queued behind it: each is a hint, and the hub reads on its own anyway.
MAX_IN_FLIGHT = 64

Announce = Callable[[frozenset[UUID]], None]


def announcing(announce: Announce) -> dict[str, Any]:
    """The ``info`` for a session factory whose commits should be announced."""
    return {ANNOUNCE: announce}


def note_appended(session: AsyncSession, run_id: UUID) -> None:
    if ANNOUNCE in session.info:
        session.info.setdefault(_APPENDED, set()).add(run_id)


@event.listens_for(Session, "after_commit")
def _announce_committed(session: Session) -> None:  # pyright: ignore[reportUnusedFunction]
    appended: set[UUID] | None = session.info.pop(_APPENDED, None)
    announce: Announce | None = session.info.get(ANNOUNCE)
    if not appended or announce is None:
        return
    try:
        announce(frozenset(appended))
    except Exception:
        # The commit has happened; failing here would report a write that
        # succeeded as one that did not.
        logger.warning("run event announcement failed", exc_info=True)


@event.listens_for(Session, "after_rollback")
def _forget_rolled_back(session: Session) -> None:  # pyright: ignore[reportUnusedFunction]
    # Fires for the outer transaction only. Events appended inside a savepoint
    # that was rolled back are still announced when the outer one commits,
    # which costs a subscriber one read that finds nothing new.
    session.info.pop(_APPENDED, None)


class RedisRunEventPush:
    """Both ends of the announcement channel. Every failure is swallowed."""

    def __init__(self, url: str, channel: str = CHANNEL) -> None:
        self._url = url
        self._channel = channel
        self._client: Redis | None = None
        self._pending: set[asyncio.Task[None]] = set()
        self._failing = False

    def announce(self, run_ids: frozenset[UUID]) -> None:
        """Schedule the publish; called from the synchronous commit hook."""
        if len(self._pending) >= MAX_IN_FLIGHT:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._publish(run_ids))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def listen(
        self, on_run: Callable[[UUID], None], on_health: Callable[[bool], None]
    ) -> None:
        """Hand every announced Run to ``on_run`` until cancelled.

        ``on_health(True)`` only once the subscription is confirmed, and
        ``on_health(False)`` as soon as it is lost: while it is down nothing
        published reaches this process, so the hub must not wait to be told.
        """
        while True:
            healthy = False
            client: Redis = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
                self._url, socket_connect_timeout=1, socket_timeout=PING_SECONDS
            )
            subscription: Any = client.pubsub()  # pyright: ignore[reportUnknownMemberType]
            try:
                await subscription.subscribe(self._channel)
                heard = pinged = monotonic()
                while True:
                    now = monotonic()
                    if now - heard > 2 * PING_SECONDS:
                        raise ConnectionError("the push channel went quiet")
                    if now - pinged >= PING_SECONDS:
                        await subscription.ping()
                        pinged = now
                    message = await subscription.get_message(timeout=1.0)
                    if message is None:
                        continue
                    heard = monotonic()
                    kind = message.get("type")
                    if kind == "subscribe" and not healthy:
                        healthy = True
                        on_health(True)
                    elif kind == "message":
                        self._deliver(message.get("data"), on_run)
            except asyncio.CancelledError:
                raise
            except Exception:
                if healthy:
                    logger.warning("run event push lost; event streams poll", exc_info=True)
            finally:
                if healthy:
                    on_health(False)
                with contextlib.suppress(Exception):
                    await subscription.aclose()
                with contextlib.suppress(Exception):
                    await client.aclose()
            await asyncio.sleep(RECONNECT_SECONDS)

    async def close(self) -> None:
        for task in list(self._pending):
            task.cancel()
        with contextlib.suppress(Exception):
            if self._client is not None:
                await self._client.aclose()
        self._client = None

    async def _publish(self, run_ids: frozenset[UUID]) -> None:
        try:
            client = self._connect()
            for run_id in run_ids:
                await client.publish(  # pyright: ignore[reportUnknownMemberType]
                    self._channel, str(run_id)
                )
        except Exception:
            # Once per outage, not once per commit: a Worker commits events
            # every round, and the degraded CI run has no Redis at all.
            if not self._failing:
                logger.warning("run event publish failed; subscribers will poll", exc_info=True)
            self._failing = True
            return
        if self._failing:
            logger.info("run event publish recovered")
            self._failing = False

    def _connect(self) -> Redis:
        if self._client is None:
            self._client = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
                self._url, socket_connect_timeout=1, socket_timeout=2
            )
        return self._client

    @staticmethod
    def _deliver(data: Any, on_run: Callable[[UUID], None]) -> None:
        if isinstance(data, bytes):
            data = data.decode(errors="replace")
        if not isinstance(data, str):
            return
        try:
            run_id = UUID(data)
        except ValueError:
            return
        on_run(run_id)
