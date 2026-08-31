"""连接的生死要留下痕迹。

断线时长是将来验证补投语义的唯一依据——拔了网线之后，要知道断了多久，
才能判断那段时间的消息有没有被补发。

**这里的回调必须从另一个线程上的另一个事件循环发起。** 上一轮的测试直接
`await` 适配器的私有方法，于是整类「回调不在 scheduler 的事件循环上」的
缺陷对它结构性不可见：`lark-oapi` 在 import 时就建了一个模块级 loop
（`lark_oapi/ws/client.py`），掉线回调在那个 loop 的线程里同步调用，消息
回调走 `lark-channel-bg` 线程上的**又一个** loop（`channel.py` 的
`schedule`）。用 scheduler 自己的 loop 建的 engine 去开 session，在这两个
loop 上都会抛 `RuntimeError: ... attached to a different loop`，而适配器的
`except Exception` 会把它咽下去——数据库里一行都不会多。所以这里的断言全部
落在「数据库里真的多了行」，触发全部经由 `_FakeChannel` 从外来 loop 发起。
"""

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lark_oapi.channel import (  # pyright: ignore[reportMissingTypeStubs]
    Events,
    FeishuChannelError,
    FeishuChannelErrorCode,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

# `_long_connections`、`_connection_event_recorder` 和 `_supervised_connection`
# 是 `_scheduler` 建连接、写审计、以及「一条坏绑定不拖垮主循环」这三件事的
# 全部实现，没有公开 API（计划 Task 4 自己写的：「无新公开接口」），所以只能
# 像别处的集成测试伸手拿 store 的 `_session` 那样伸手拿它们。
from tiny_hermes.api.cli import (
    _connection_event_recorder,  # noqa: SLF001 # pyright: ignore[reportPrivateUsage]
    _long_connections,  # noqa: SLF001 # pyright: ignore[reportPrivateUsage]
    _supervised_connection,  # noqa: SLF001 # pyright: ignore[reportPrivateUsage]
)
from tiny_hermes.channels.application.webhook_service import Claimed, Unreadable
from tiny_hermes.channels.infrastructure import feishu_long_connection
from tiny_hermes.channels.infrastructure.feishu_long_connection import (
    FeishuLongConnection,
    LongConnectionBinding,
)
from tiny_hermes.channels.infrastructure.sql_binding_store import SqlChannelBindingStore
from tiny_hermes.secrets.domain.envelope import seal
from tiny_hermes.shared.config import Settings

from ..conftest import VALID_SPEC

#: Matches the `settings` fixture's `tiny_hermes_kek` in conftest.py
#: ("AAAA...=", 32 zero bytes decoded) — the point of sealing a secret with
#: it is exercising `_long_connections`'s real `CredentialResolver`, the
#: same one production wires, rather than a resolver built around a
#: test-only key.
KEK = b"\x00" * 32

#: How long the fake socket stays down. Long enough that a `down_seconds`
#: hardcoded to `0.0` — or one measured from the wrong instant — cannot pass
#: the assertion below, short enough not to slow the suite down.
OUTAGE_SECONDS = 0.2


@dataclass
class _Frame:
    """`on_frame` 只读 `.raw` —— 见 `feishu_long_connection._envelope_of`。"""

    raw: dict[str, Any]


def _text_message_envelope(body: str, *, event_id: str) -> dict[str, Any]:
    return {
        "header": {"event_id": event_id},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_zhang"}},
            "message": {"content": json.dumps({"text": body})},
        },
    }


@pytest.fixture
async def store(engine: AsyncEngine) -> AsyncIterator[SqlChannelBindingStore]:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        yield SqlChannelBindingStore(db)


async def _connection_events(store: SqlChannelBindingStore) -> list[dict[str, Any]]:
    rows = (
        await store._session.execute(  # noqa: SLF001 # pyright: ignore[reportPrivateUsage]
            text(
                "SELECT action, result, context FROM audit_events"
                " WHERE resource_type = 'channel_binding'"
                " AND action LIKE 'channel.long_connection.%'"
                " ORDER BY created_at, id"
            )
        )
    ).all()
    return [
        {
            "kind": str(row.action).rsplit(".", 1)[-1],
            "result": row.result,
            "context": cast("dict[str, Any]", row.context or {}),
        }
        for row in rows
    ]


def _only(events: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    """Finds the one row of a kind instead of indexing into the list.

    `_connection_events` orders by `created_at, id`, and `id` is a random
    uuid4 — two rows written inside the same microsecond come back in an
    order decided by a coin flip, so `events[1]` names a different row on
    different runs. Repo rule: assert by key, never by position.
    """
    matching = [event for event in events if event["kind"] == kind]
    assert len(matching) == 1, f"expected exactly one {kind!r} row, got {matching}"
    return matching[0]


async def _claimed_event_count(
    store: SqlChannelBindingStore, binding_id: UUID, channel_event_id: str
) -> int:
    found = await store._session.execute(  # noqa: SLF001 # pyright: ignore[reportPrivateUsage]
        text(
            "SELECT count(*) FROM channel_events"
            " WHERE channel_binding_id = :b AND channel_event_id = :e"
        ),
        {"b": binding_id, "e": channel_event_id},
    )
    return int(found.scalar_one())


async def _stored_secret(engine: AsyncEngine, workspace_id: str, value: str) -> UUID:
    """One active workspace Secret, sealed the way the service seals them.

    Reuses `test_image_source.py`'s pattern: a real KEK and a real Secret
    row, because the point of `seeded_bindings_of_both_transports` is
    exercising the path that unwraps a stored Secret, not the env-var
    branch of `CredentialResolver` that needs no store at all.
    """
    secret_id = uuid4()
    envelope = seal(value.encode("utf-8"), KEK, "test-key")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO secrets (id, name, scope, workspace_id, status, mask,"
                " ciphertext, nonce, wrapped_dek, wrap_nonce, key_id, created_at,"
                " updated_at) VALUES (:i, :name, 'workspace', :w,"
                " 'active', '****', :c, :n, :d, :dn, 'test-key', now(), now())"
            ),
            {
                "i": secret_id,
                # `secrets` enforces one name per workspace — a distinct
                # name per call so a fixture that seeds two bindings in one
                # workspace (webhook and long_connection) can give each its
                # own secret rather than colliding on a shared literal.
                "name": f"feishu-app-secret-{secret_id.hex[:8]}",
                "w": UUID(workspace_id),
                "c": envelope.ciphertext,
                "n": envelope.nonce,
                "d": envelope.wrapped_dek,
                "dn": envelope.wrap_nonce,
            },
        )
    return secret_id


def _second_agent(client: TestClient, scope: dict[str, str]) -> str:
    """A second published Agent, needed only because `channel_bindings`
    enforces one binding per `(workspace_id, channel, agent_id)`: two
    bindings in the same workspace on the same channel cannot share an
    Agent, regardless of transport.
    """
    agent_id = str(
        client.post(
            "/api/v1/agents", headers=scope, json={"name": "Second", "alias": "second"}
        ).json()["id"]
    )
    draft = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": VALID_SPEC},
    )
    assert draft.status_code == 200
    published = client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=scope,
        json={"expected_revision": draft.json()["revision"]},
    )
    assert published.status_code == 201
    return agent_id


async def _seed_binding(
    engine: AsyncEngine,
    workspace_id: str,
    agent_id: str,
    *,
    transport: str,
    app_id: str | None = None,
    app_secret_ref: str | None = None,
) -> UUID:
    binding_id = uuid4()
    async with engine.begin() as connection:
        owner = await connection.execute(text("SELECT id FROM users LIMIT 1"))
        await connection.execute(
            text(
                "INSERT INTO channel_bindings"
                " (id, workspace_id, channel, agent_id, status, created_by,"
                "  created_at, encrypt_key_ref, transport, app_id, app_secret_ref)"
                " VALUES (:i, :w, 'feishu', :a, 'active', :u, now(), 'K', :t,"
                "  :app_id, :sec)"
            ),
            {
                "i": binding_id,
                "w": UUID(workspace_id),
                "a": UUID(agent_id),
                "u": owner.scalar_one(),
                "t": transport,
                "app_id": app_id,
                "sec": app_secret_ref,
            },
        )
    return binding_id


@pytest.fixture
async def seeded_bindings_of_both_transports(
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    client: TestClient,
    scope: dict[str, str],
) -> tuple[UUID, UUID]:
    """One `webhook` binding and one `long_connection` binding with a real,
    resolvable secret.

    The webhook one has to actually exist: this fixture backs
    `test_only_long_connection_bindings_get_a_connection`, whose entire job
    is proving the filter excludes it. A fixture with only a long_connection
    binding would let that test pass for the wrong reason — see Step 5 of
    the plan, which requires removing the filter and watching this test go
    red before trusting it.

    Two different Agents, not one: `channel_bindings` enforces one binding
    per `(workspace_id, channel, agent_id)`, so two bindings on the same
    channel in the same workspace need two Agents regardless of transport.

    The webhook binding gets real, resolvable credentials too — not just a
    `transport` value. `_long_connections` also skips a binding whose
    credentials do not resolve, so a webhook binding with no `app_id`/
    `app_secret_ref` would be excluded by *that* check even with the
    transport filter deleted, and Step 5's destructive check (remove the
    filter, expect this test to go red) would pass for the wrong reason —
    exactly the "fixture proves nothing" trap the plan calls out.
    """
    webhook_secret_ref = await _stored_secret(engine, workspace_id, "webhook-secret")
    webhook_id = await _seed_binding(
        engine,
        workspace_id,
        published_agent,
        transport="webhook",
        app_id="cli_webhook",
        app_secret_ref=str(webhook_secret_ref),
    )
    second_agent = _second_agent(client, scope)
    secret_ref = await _stored_secret(engine, workspace_id, "app-secret-value")
    long_id = await _seed_binding(
        engine,
        workspace_id,
        second_agent,
        transport="long_connection",
        app_id="cli_long",
        app_secret_ref=str(secret_ref),
    )
    return webhook_id, long_id


@pytest.fixture
async def binding_with_bad_credentials(
    engine: AsyncEngine, workspace_id: str, published_agent: str
) -> UUID:
    """`app_secret_ref` names a Secret id nothing ever inserted — the
    credential-resolution failure `_long_connections` has to survive
    without raising, the same way a wrong or revoked app secret would in a
    real deployment.
    """
    return await _seed_binding(
        engine,
        workspace_id,
        published_agent,
        transport="long_connection",
        app_id="cli_bad",
        app_secret_ref=str(uuid4()),
    )


@pytest.fixture
def scheduler_connections(
    settings: Settings, engine: AsyncEngine
) -> Callable[[], Awaitable[tuple[FeishuLongConnection, ...]]]:
    """Wraps `_long_connections` — the same function `_scheduler` calls at
    startup — bound to this test's own engine rather than the real one
    `get_settings()` would build from the process environment.

    Going through it (rather than constructing a `FeishuLongConnection` by
    hand) is what makes the tests below notice if production stops passing
    `record=`: an adapter built here carries whatever wiring `_scheduler`
    gives it, no more.
    """
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def build() -> tuple[FeishuLongConnection, ...]:
        return await _long_connections(settings, sessions)

    return build


class _LoopThread:
    """One event loop on one non-scheduler thread.

    `lark-oapi` has two of these and neither is the scheduler's: the module
    level `loop` created at import time in `lark_oapi/ws/client.py`, and the
    `lark-channel-bg` loop `FeishuChannel` spawns for `schedule()`. A
    coroutine started on either one cannot touch a SQLAlchemy engine built
    on the scheduler's loop.
    """

    def __init__(self, name: str) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, name=name, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, coro: Any) -> "Future[None]":
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()


class _FakeChannel:
    """Stands in for `FeishuChannel`, calling handlers the way it does.

    Two things matter and only two: the reconnect handlers are plain `h()`
    calls made from inside a coroutine on the ws loop (`_notify_reconnecting`
    in `channel.py`, reached from `_receive_message_loop` in `ws/client.py`),
    and the message handler is *awaited* on the `lark-channel-bg` loop
    (`_invoke` awaits an awaitable result). Both loops belong to other
    threads. Everything else about the SDK is left out on purpose — the
    adapter only ever calls `on`, `connect_until_ready` and `disconnect`.
    """

    def __init__(
        self,
        *,
        connect_error: BaseException | None = None,
        reconnecting_before_error: bool = False,
    ) -> None:
        self._handlers: dict[str, list[Any]] = {}
        self._connect_error = connect_error
        self._reconnecting_before_error = reconnecting_before_error
        self._ws = _LoopThread("fake-lark-ws")
        self._bg = _LoopThread("fake-lark-channel-bg")
        self._closed = False
        self.connects = 0
        self.disconnects = 0
        self.ready = False

    def on(self, event: str, handler: Any) -> None:
        self._handlers.setdefault(event, []).append(handler)

    # ASYNC109: the parameter is not this fake's design, it is
    # `FeishuChannel.connect_until_ready`'s signature, which `run()` calls
    # with `timeout=30`. Renaming it would make the stand-in stop standing in.
    async def connect_until_ready(self, *, timeout: float | None = None) -> None:  # noqa: ASYNC109
        del timeout
        self.connects += 1
        if self._connect_error is not None:
            if self._reconnecting_before_error:
                # `ws/client.py`'s `start` calls `_reconnect()` when the very
                # first connect fails too, so `on_reconnecting` fires for a
                # socket that was never up.
                await self._notify(Events.RECONNECTING)
            raise self._connect_error
        self.ready = True

    async def disconnect(self) -> None:
        self.disconnects += 1
        if self._closed:
            return
        self._closed = True
        self._ws.close()
        self._bg.close()

    async def _notify(self, event: str) -> None:
        async def _call() -> None:
            for handler in self._handlers.get(event, []):
                handler()

        await asyncio.wrap_future(self._ws.submit(_call()))

    async def signal_drop(self) -> None:
        await self._notify(Events.RECONNECTING)

    async def drop_and_recover(self, *, outage: float) -> None:
        await self.signal_drop()
        await asyncio.sleep(outage)
        await self._notify(Events.RECONNECTED)

    async def deliver_frame(self, frame: _Frame) -> None:
        async def _invoke() -> None:
            for handler in self._handlers.get(Events.MESSAGE, []):
                await handler(frame)

        await asyncio.wrap_future(self._bg.submit(_invoke()))


class _FakeChannels:
    """The `FeishuChannel` constructor `run()` calls, plus the handle the
    test needs on whatever that call produced."""

    def __init__(
        self,
        *,
        connect_error: BaseException | None = None,
        reconnecting_before_error: bool = False,
    ) -> None:
        self._connect_error = connect_error
        self._reconnecting_before_error = reconnecting_before_error
        self.channels: list[_FakeChannel] = []

    def __call__(self, *, app_id: str, app_secret: str) -> _FakeChannel:
        del app_id, app_secret
        channel = _FakeChannel(
            connect_error=self._connect_error,
            reconnecting_before_error=self._reconnecting_before_error,
        )
        self.channels.append(channel)
        return channel

    @property
    def connects(self) -> int:
        return sum(channel.connects for channel in self.channels)

    @property
    def disconnects(self) -> int:
        return sum(channel.disconnects for channel in self.channels)

    async def _until(self, ready: Callable[[], bool], what: str) -> None:
        deadline = time.monotonic() + 5.0
        while not ready():
            assert time.monotonic() < deadline, f"timed out waiting for {what}"
            await asyncio.sleep(0.005)

    async def connected(self) -> _FakeChannel:
        await self._until(
            lambda: any(channel.ready for channel in self.channels), "a connected channel"
        )
        return next(channel for channel in self.channels if channel.ready)

    async def attempted(self, times: int) -> None:
        await self._until(lambda: self.connects >= times, f"{times} connect attempts")


@pytest.fixture
def sdk_channels(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _FakeChannels]:
    """Replaces the `FeishuChannel` the adapter constructs inside `run()`.

    Patched on the module rather than injected through a constructor
    argument: `run()` builds its own channel, and a seam added only so a
    test could reach in would be a seam production never uses.
    """

    def install(**kwargs: Any) -> _FakeChannels:
        channels = _FakeChannels(**kwargs)
        monkeypatch.setattr(feishu_long_connection, "FeishuChannel", channels)
        return channels

    return install


async def test_only_long_connection_bindings_get_a_connection(
    scheduler_connections: Callable[[], Awaitable[tuple[FeishuLongConnection, ...]]],
    seeded_bindings_of_both_transports: tuple[UUID, UUID],
) -> None:
    webhook_id, long_id = seeded_bindings_of_both_transports
    del webhook_id  # only the binding this test excludes, not one it asserts on

    started = await scheduler_connections()

    assert [b.binding_id for b in started] == [long_id]


async def test_a_drop_signalled_on_the_sdk_thread_is_recorded_with_how_long_it_lasted(
    store: SqlChannelBindingStore,
    scheduler_connections: Callable[[], Awaitable[tuple[FeishuLongConnection, ...]]],
    seeded_bindings_of_both_transports: tuple[UUID, UUID],
    sdk_channels: Callable[..., _FakeChannels],
) -> None:
    channels = sdk_channels()
    (connection,) = await scheduler_connections()
    stop = asyncio.Event()
    running = asyncio.create_task(connection.run(stop))

    channel = await channels.connected()
    await channel.drop_and_recover(outage=OUTAGE_SECONDS)
    stop.set()
    await running

    events = await _connection_events(store)
    disconnected = _only(events, "disconnected")
    reconnected = _only(events, "reconnected")
    # The outage is still open when the first row is written, so there is no
    # number yet — but the key is there, which is what tells a reader "not
    # known" apart from a row that carries no context at all.
    assert disconnected["context"] == {"down_seconds": None}
    assert reconnected["context"]["down_seconds"] >= OUTAGE_SECONDS / 2


async def test_a_frame_arriving_on_the_sdk_loop_reaches_channel_events(
    store: SqlChannelBindingStore,
    scheduler_connections: Callable[[], Awaitable[tuple[FeishuLongConnection, ...]]],
    seeded_bindings_of_both_transports: tuple[UUID, UUID],
    sdk_channels: Callable[..., _FakeChannels],
) -> None:
    _webhook_id, long_id = seeded_bindings_of_both_transports
    channels = sdk_channels()
    (connection,) = await scheduler_connections()
    stop = asyncio.Event()
    running = asyncio.create_task(connection.run(stop))

    channel = await channels.connected()
    await channel.deliver_frame(
        _Frame(raw=_text_message_envelope("hello", event_id="lc-1"))
    )
    stop.set()
    await running

    # Written-and-reachable, not "the adapter did not raise": `on_frame`
    # swallows every exception, so the only evidence that the claim actually
    # happened is the row.
    assert await _claimed_event_count(store, long_id, "lc-1") == 1


async def test_run_does_not_return_before_a_recording_it_started_is_written(
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    sdk_channels: Callable[..., _FakeChannels],
) -> None:
    """`run()`'s `finally` claims it waits for recordings still in flight.

    Stopping in the same tick the SDK signalled the drop is when that claim
    is load-bearing. The recorder is deliberately slower than the shutdown
    path so the two orders are distinguishable: without a real wait, `run()`
    returns while the write is still going, and the assertion below reads
    `["returned", "recorded"]`.
    """
    binding_id = await _seed_binding(
        engine, workspace_id, published_agent, transport="long_connection", app_id="cli_x"
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    written = _connection_event_recorder(sessions, UUID(workspace_id))
    order: list[str] = []

    async def slow_record(binding_id: UUID, kind: str, down_seconds: float | None) -> None:
        await asyncio.sleep(0.05)
        await written(binding_id, kind, down_seconds)
        order.append("recorded")

    async def never_delivers(binding_id: UUID, envelope: dict[str, Any]) -> Claimed | Unreadable:
        raise AssertionError("this test never sends a frame")

    channels = sdk_channels()
    connection = FeishuLongConnection(
        LongConnectionBinding(binding_id=binding_id, app_id="cli_x", app_secret="s"),
        never_delivers,
        record=slow_record,
    )
    stop = asyncio.Event()
    running = asyncio.create_task(connection.run(stop))

    channel = await channels.connected()
    await channel.signal_drop()
    stop.set()
    await running
    order.append("returned")

    assert order == ["recorded", "returned"]


async def test_a_connection_that_cannot_connect_does_not_stop_the_scheduler(
    scheduler_connections: Callable[[], Awaitable[tuple[FeishuLongConnection, ...]]],
    seeded_bindings_of_both_transports: tuple[UUID, UUID],
    sdk_channels: Callable[..., _FakeChannels],
) -> None:
    # 一个连不上的绑定不能拖垮主循环——它还负责发回复、发卡片、回执。
    channels = sdk_channels(
        connect_error=FeishuChannelError(
            FeishuChannelErrorCode.NOT_CONNECTED, "WebSocket connect failed"
        )
    )
    (connection,) = await scheduler_connections()
    stop = asyncio.Event()
    ticks = 0

    async def main_loop() -> None:
        """Stands in for `runtime.run_forever` — the half of `_scheduler`'s
        `gather` that must survive a connection failing over and over."""
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.005)

    async def stop_once_it_has_retried() -> None:
        await channels.attempted(2)
        stop.set()

    await asyncio.gather(
        main_loop(),
        _supervised_connection(connection, stop, first_delay=0.01, max_delay=0.02),
        stop_once_it_has_retried(),
    )

    assert channels.connects >= 2, "gave up instead of retrying"
    assert ticks >= 2, "the main loop stopped ticking while the connection failed"


async def test_a_failed_connect_leaves_a_trace_and_no_leaked_channel(
    store: SqlChannelBindingStore,
    scheduler_connections: Callable[[], Awaitable[tuple[FeishuLongConnection, ...]]],
    seeded_bindings_of_both_transports: tuple[UUID, UUID],
    sdk_channels: Callable[..., _FakeChannels],
) -> None:
    # `FeishuChannel.start` has already spawned the `lark-channel-bg` thread
    # and its loop by the time a connect fails, and `_finish_failed_start`
    # only resets `_started` — so a failure that skips `disconnect()` leaks a
    # thread and a loop on every retry, forever.
    channels = sdk_channels(
        connect_error=FeishuChannelError(
            FeishuChannelErrorCode.NOT_CONNECTED, "WebSocket connect failed"
        ),
        reconnecting_before_error=True,
    )
    (connection,) = await scheduler_connections()

    with pytest.raises(FeishuChannelError):
        await connection.run(asyncio.Event())

    assert channels.disconnects == 1
    events = await _connection_events(store)
    failed = _only(events, "connect_failed")
    assert failed["result"] == "failed"
    # A socket that never came up has not "disconnected", and the outage the
    # word implies has no start instant to measure a later `down_seconds`
    # from. §19.2's redelivery check reads these rows.
    assert [event["kind"] for event in events] == ["connect_failed"]


async def test_a_binding_whose_secret_cannot_be_resolved_is_skipped(
    scheduler_connections: Callable[[], Awaitable[tuple[FeishuLongConnection, ...]]],
    binding_with_bad_credentials: UUID,
) -> None:
    del binding_with_bad_credentials  # the row this test expects to be dropped

    started = await scheduler_connections()

    assert started == ()
