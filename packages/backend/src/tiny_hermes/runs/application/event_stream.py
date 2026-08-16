"""One database poll per Run, fan-out to every live SSE subscriber.

§24.1 holds 500 connections on one Run and expects a 5s cadence. A poll per
connection serialises on the pool and opens 11–13s gaps. The catch-up of each
subscriber is still its own read (different cursors); only the live hold is
shared.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from tiny_hermes.runs.ports.store import RunEventRecord, RunEventWindow
from tiny_hermes.tenancy.domain.models import Actor

logger = logging.getLogger(__name__)

Poll = Callable[
    [UUID, Actor, UUID, int],
    Awaitable[tuple[Sequence[RunEventRecord], RunEventWindow]],
]
Encode = Callable[[RunEventRecord], bytes]
Disconnected = Callable[[], Awaitable[bool]]


@dataclass
class _Member:
    queue: asyncio.Queue[bytes | None]
    cursor: int


@dataclass
class _Room:
    workspace_id: UUID
    actor: Actor
    run_id: UUID
    encode: Encode
    members: list[_Member] = field(default_factory=list[_Member])
    task: asyncio.Task[None] | None = None


class EventStreamHub:
    def __init__(self, poll: Poll, poll_seconds: float = 0.5) -> None:
        self._poll = poll
        self._poll_seconds = poll_seconds
        self._rooms: dict[tuple[UUID, UUID], _Room] = {}
        self._lock = asyncio.Lock()

    async def frames(
        self,
        workspace_id: UUID,
        actor: Actor,
        run_id: UUID,
        after: int,
        *,
        disconnected: Disconnected,
        heartbeat_seconds: float,
        encode: Encode,
    ) -> AsyncIterator[bytes]:
        cursor = after
        while not await disconnected():
            records, window = await self._poll(workspace_id, actor, run_id, cursor)
            for record in records:
                cursor = record.sequence
                yield encode(record)
            if records:
                continue
            if window.is_terminal and cursor + 1 >= window.next_sequence:
                return
            break

        member = _Member(queue=asyncio.Queue(maxsize=256), cursor=cursor)
        await self._join(workspace_id, actor, run_id, member, encode)
        try:
            while not await disconnected():
                try:
                    item = await asyncio.wait_for(
                        member.queue.get(), timeout=heartbeat_seconds
                    )
                except TimeoutError:
                    yield b": heartbeat\n\n"
                    continue
                if item is None:
                    return
                yield item
        finally:
            await self._leave(workspace_id, run_id, member)

    async def _join(
        self,
        workspace_id: UUID,
        actor: Actor,
        run_id: UUID,
        member: _Member,
        encode: Encode,
    ) -> None:
        key = (workspace_id, run_id)
        async with self._lock:
            room = self._rooms.get(key)
            if room is None:
                room = _Room(workspace_id, actor, run_id, encode)
                self._rooms[key] = room
            room.members.append(member)
            if room.task is None or room.task.done():
                room.task = asyncio.create_task(self._run_room(key))

    async def _leave(
        self, workspace_id: UUID, run_id: UUID, member: _Member
    ) -> None:
        key = (workspace_id, run_id)
        task: asyncio.Task[None] | None = None
        async with self._lock:
            room = self._rooms.get(key)
            if room is None:
                return
            if member in room.members:
                room.members.remove(member)
            if not room.members:
                task = room.task
                self._rooms.pop(key, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run_room(self, key: tuple[UUID, UUID]) -> None:
        room = self._rooms.get(key)
        if room is None:
            return
        try:
            while True:
                async with self._lock:
                    members = list(room.members)
                if not members:
                    return
                cursor = min(member.cursor for member in members)
                try:
                    records, window = await self._poll(
                        room.workspace_id, room.actor, room.run_id, cursor
                    )
                except Exception:
                    logger.exception("shared event poll failed")
                    await asyncio.sleep(self._poll_seconds)
                    continue
                for member in members:
                    for record in records:
                        if record.sequence <= member.cursor:
                            continue
                        member.cursor = record.sequence
                        self._offer(member, room.encode(record))
                    if window.is_terminal and member.cursor + 1 >= window.next_sequence:
                        self._offer(member, None)
                if not records:
                    await asyncio.sleep(self._poll_seconds)
        finally:
            async with self._lock:
                current = self._rooms.get(key)
                if current is not None and current is room and not current.members:
                    self._rooms.pop(key, None)

    @staticmethod
    def _offer(member: _Member, item: bytes | None) -> None:
        try:
            member.queue.put_nowait(item)
        except asyncio.QueueFull:
            if item is None:
                return
            with contextlib.suppress(asyncio.QueueFull):
                member.queue.put_nowait(None)
