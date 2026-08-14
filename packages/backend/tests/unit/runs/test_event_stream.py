"""One poll fans out. 500 subscribers must not mean 500 queries."""

import asyncio
from datetime import UTC, datetime
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
