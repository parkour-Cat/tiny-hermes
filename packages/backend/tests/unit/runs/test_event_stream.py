"""One poll fans out. 500 subscribers must not mean 500 queries."""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from tiny_hermes.runs.application.event_stream import EventStreamHub
from tiny_hermes.runs.domain.models import RunEventType
from tiny_hermes.runs.ports.store import RunEventRecord, RunEventWindow
from tiny_hermes.tenancy.domain.models import Actor

ACTOR = Actor.new(False)
WORKSPACE = uuid4()
RUN = uuid4()


def _record(sequence: int) -> RunEventRecord:
    return RunEventRecord(
        sequence=sequence,
        event_type=RunEventType.RUN_CREATED,
        occurred_at=datetime.now(UTC),
        payload={},
    )


def _window(*, nxt: int, terminal: bool = False) -> RunEventWindow:
    return RunEventWindow(earliest_sequence=1, next_sequence=nxt, is_terminal=terminal)


async def test_two_live_subscribers_share_one_poll_per_tick() -> None:
    polls: list[int] = []
    live = asyncio.Event()

    async def poll(
        _ws: UUID, _actor: Actor, _run: UUID, cursor: int
    ) -> tuple[list[RunEventRecord], RunEventWindow]:
        polls.append(cursor)
        rows = [_record(1), _record(2)]
        nxt = 3
        if live.is_set():
            rows.append(_record(3))
            nxt = 4
        return [row for row in rows if row.sequence > cursor], _window(nxt=nxt)

    hub = EventStreamHub(poll, poll_seconds=0.01)
    seen_a: list[int] = []
    seen_b: list[int] = []
    stop = False

    async def disconnected() -> bool:
        return stop

    async def take(into: list[int]) -> None:
        async for chunk in hub.frames(
            WORKSPACE,
            ACTOR,
            RUN,
            0,
            disconnected=disconnected,
            heartbeat_seconds=5.0,
            encode=lambda record: str(record.sequence).encode(),
        ):
            if chunk.startswith(b":"):
                continue
            into.append(int(chunk.decode()))
            if 3 in into:
                return

    first = asyncio.create_task(take(seen_a))
    second = asyncio.create_task(take(seen_b))
    for _ in range(50):
        if seen_a[:2] == [1, 2] and seen_b[:2] == [1, 2]:
            break
        await asyncio.sleep(0.01)
    before_live = len(polls)
    live.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=2.0)
    stop = True
    live_polls = polls[before_live:]
    assert seen_a == [1, 2, 3]
    assert seen_b == [1, 2, 3]
    # The hold that produced sequence 3 is one shared read, not one per subscriber.
    assert live_polls.count(2) == 1


# -- pushed ---------------------------------------------------------------


class _Run:
    """A Run whose events appear when the test says so, counting every read."""

    def __init__(self) -> None:
        self.polls = 0
        self.last = 1

    async def poll(
        self, _ws: UUID, _actor: Actor, _run: UUID, cursor: int
    ) -> tuple[list[RunEventRecord], RunEventWindow]:
        self.polls += 1
        rows = [_record(sequence) for sequence in range(cursor + 1, self.last + 1)]
        return rows, _window(nxt=self.last + 1)


async def _subscribe(hub: EventStreamHub, into: list[int]) -> None:
    async def connected() -> bool:
        return False

    async for chunk in hub.frames(
        WORKSPACE,
        ACTOR,
        RUN,
        0,
        disconnected=connected,
        heartbeat_seconds=30.0,
        encode=lambda record: str(record.sequence).encode(),
    ):
        into.append(int(chunk.decode()))


async def _until(condition: Any, within: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + within
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.005)


async def test_a_pushed_room_does_not_read_again_until_it_is_told_to() -> None:
    run = _Run()
    hub = EventStreamHub(run.poll, poll_seconds=0.01, pushed_poll_seconds=60)
    hub.pushed(True)
    seen: list[int] = []
    task = asyncio.create_task(_subscribe(hub, seen))
    try:
        await _until(lambda: seen == [1])
        await asyncio.sleep(0.2)  # twenty unpushed intervals
        idle = run.polls

        run.last = 2
        hub.wake(RUN)
        await _until(lambda: seen == [1, 2])
    finally:
        task.cancel()

    # Catch-up, the room's first read, and nothing more until the wake.
    assert idle <= 3
    assert run.polls - idle <= 2


async def test_without_a_push_channel_a_room_keeps_polling() -> None:
    """Redis is an optimisation: down or never configured, nothing waits longer."""
    run = _Run()
    hub = EventStreamHub(run.poll, poll_seconds=0.01, pushed_poll_seconds=60)
    seen: list[int] = []
    task = asyncio.create_task(_subscribe(hub, seen))
    try:
        await _until(lambda: seen == [1])
        run.last = 2
        await _until(lambda: seen == [1, 2])
    finally:
        task.cancel()


async def test_a_pushed_room_still_reads_now_and_then() -> None:
    """A notification lost on the way costs one long interval, not the event."""
    run = _Run()
    hub = EventStreamHub(run.poll, poll_seconds=0.01, pushed_poll_seconds=0.1)
    hub.pushed(True)
    seen: list[int] = []
    task = asyncio.create_task(_subscribe(hub, seen))
    try:
        await _until(lambda: seen == [1])
        run.last = 2  # and nobody says so
        await _until(lambda: seen == [1, 2])
    finally:
        task.cancel()


async def test_losing_the_push_channel_falls_back_to_polling_at_once() -> None:
    run = _Run()
    hub = EventStreamHub(run.poll, poll_seconds=0.01, pushed_poll_seconds=60)
    hub.pushed(True)
    seen: list[int] = []
    task = asyncio.create_task(_subscribe(hub, seen))
    try:
        await _until(lambda: seen == [1])
        await asyncio.sleep(0.05)  # the room is in its long wait
        hub.pushed(False)
        run.last = 2
        await _until(lambda: seen == [1, 2])
    finally:
        task.cancel()


async def test_regaining_the_push_channel_reads_every_room_once() -> None:
    """Whatever was published while the channel was down was never heard."""
    run = _Run()
    hub = EventStreamHub(run.poll, poll_seconds=60, pushed_poll_seconds=60)
    seen: list[int] = []
    task = asyncio.create_task(_subscribe(hub, seen))
    try:
        await _until(lambda: seen == [1])
        await asyncio.sleep(0.05)
        run.last = 2
        hub.pushed(True)
        await _until(lambda: seen == [1, 2])
    finally:
        task.cancel()


async def test_a_wake_during_a_read_is_not_lost() -> None:
    """The read already under way may have missed the row it is about."""
    gate = asyncio.Event()
    reading = asyncio.Event()
    run = _Run()

    async def slow_poll(
        ws: UUID, actor: Actor, run_id: UUID, cursor: int
    ) -> tuple[list[RunEventRecord], RunEventWindow]:
        answer = await run.poll(ws, actor, run_id, cursor)
        # Reads one and two are the subscriber's own catch-up; three is the room's.
        if run.polls == 3:
            reading.set()
            await gate.wait()  # the row commits and is announced right now
        return answer

    hub = EventStreamHub(slow_poll, poll_seconds=60, pushed_poll_seconds=60)
    hub.pushed(True)
    seen: list[int] = []
    task = asyncio.create_task(_subscribe(hub, seen))
    try:
        await asyncio.wait_for(reading.wait(), timeout=2.0)
        run.last = 2
        hub.wake(RUN)
        gate.set()
        await _until(lambda: seen == [1, 2])
    finally:
        task.cancel()


async def test_waking_a_run_nobody_watches_is_harmless() -> None:
    hub = EventStreamHub(_Run().poll)
    hub.wake(uuid4())
