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
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lark_oapi.channel import (  # pyright: ignore[reportMissingTypeStubs]
    Events,
    FeishuChannelError,
    FeishuChannelErrorCode,
)
from lark_oapi.channel.types import (  # pyright: ignore[reportMissingTypeStubs]
    Conversation,
    Identity,
    InboundMessage,
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
from tiny_hermes.channels.application.outbound import ChannelReplyDispatcher
from tiny_hermes.channels.infrastructure import feishu_long_connection
from tiny_hermes.channels.infrastructure.feishu_long_connection import (
    FeishuLongConnection,
    LongConnectionBinding,
)
from tiny_hermes.channels.infrastructure.sql_binding_store import SqlChannelBindingStore
from tiny_hermes.channels.infrastructure.sql_channel_store import SqlChannelStore
from tiny_hermes.model_catalog.infrastructure.credentials import CredentialResolver
from tiny_hermes.secrets.domain.envelope import seal
from tiny_hermes.secrets.infrastructure.sql_store import SqlSecretStore
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

#: The retry backoff the supervisor tests run with, in place of the real
#: 5s/300s. Two of these have to elapse before the connect that succeeds, so
#: it also doubles as the floor a measured outage has to clear.
BACKOFF_SECONDS = 0.02

#: How many failed connects a persistent outage is driven through before the
#: audit rows are counted. More than two, because "one row per attempt" and
#: "two rows per outage" are indistinguishable at two.
ATTEMPTS = 4

#: A recording slow enough that `run()`'s shutdown path is still waiting on
#: it when the late callback below fires.
SLOW_RECORD_SECONDS = 0.2

#: When the fake fires its post-`disconnect()` callback. Comfortably inside
#: the window above, so the race is exercised on every run rather than
#: occasionally.
LATE_SIGNAL_SECONDS = 0.05


def _inbound(body: str, *, message_id: str, open_id: str = "ou_zhang") -> InboundMessage:
    """What the SDK actually hands a `MESSAGE` handler.

    Built as the SDK's own dataclass rather than a dict shaped the way this
    repository wished the SDK worked. The hand-built dict above is the
    *webhook* envelope, and every long-connection test used to pass one to
    `deliver` directly — so nothing ever exercised `_envelope_of` against a
    real frame, and production got `no event id in either schema version`
    on the first real message.

    `raw` is the message object, not the event envelope: that is what
    `normalize/pipeline.py` puts there (`raw=msg`), and the event id never
    reaches it — `channel.py`'s `_handle_message_event` pulls it off
    `data.header.event_id` and passes it to the pipeline as a separate
    argument that `InboundMessage` does not keep.
    """
    return InboundMessage(
        id=message_id,
        create_time=0,
        conversation=Conversation(chat_id="oc_test", chat_type="p2p"),
        sender=Identity(open_id=open_id),
        raw={
            "message_id": message_id,
            "chat_id": "oc_test",
            "message_type": "text",
            "content": json.dumps({"text": body}),
        },
        content_text=body,
        raw_content_type="text",
    )


@pytest.fixture
async def store(engine: AsyncEngine) -> AsyncIterator[SqlChannelBindingStore]:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        yield SqlChannelBindingStore(db)


async def _connection_events(store: SqlChannelBindingStore) -> list[dict[str, Any]]:
    rows = (
        await store._session.execute(  # noqa: SLF001 # pyright: ignore[reportPrivateUsage]
            text(
                "SELECT action, result, context, resource_id FROM audit_events"
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
            # Which binding the row is *about*. A row that lands on the wrong
            # binding is invisible to an assertion that only reads `kind`,
            # and one of the kinds below is written for a binding that is
            # deliberately not the one holding the socket.
            "binding": row.resource_id,
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


async def _stored_secret(
    engine: AsyncEngine, workspace_id: str, value: str, *, status: str = "active"
) -> UUID:
    """One workspace Secret, sealed the way the service seals them.

    `status` is a parameter because a Secret that exists but is *disabled*
    is the failure this file's newest test is about — the console's secrets
    page can flip that column long after a binding was switched to
    `long_connection`, and `CredentialResolver.resolve` raises
    `CredentialMissing` for it exactly as it does for an id nothing
    inserted.

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
                " :status, '****', :c, :n, :d, :dn, 'test-key', now(), now())"
            ),
            {
                "i": secret_id,
                "status": status,
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
        disconnect_error: BaseException | None = None,
        late_signal_delay: float | None = None,
    ) -> None:
        self._handlers: dict[str, list[Any]] = {}
        self._connect_error = connect_error
        self._reconnecting_before_error = reconnecting_before_error
        self._disconnect_error = disconnect_error
        self._late_signal_delay = late_signal_delay
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
        if self._late_signal_delay is not None:
            # Deliberately leaves the ws loop running and queues one more
            # callback on it without waiting: `_stop_private_ws_client`
            # (`lark_oapi/channel/channel.py`) only *asks* the SDK's
            # module-level ws loop to stop and never joins it, so a callback
            # landing after `disconnect()` has returned is reachable. Whether
            # that recording survives is the adapter's problem, not the
            # caller's, which is exactly what the test using this asserts.
            self._ws.submit(self._notify_after(self._late_signal_delay, Events.RECONNECTED))
            return
        self.close_loops()
        if self._disconnect_error is not None:
            raise self._disconnect_error

    def close_loops(self) -> None:
        """Idempotent, and separate from `disconnect()` so a test that made
        `disconnect()` leave the loops running can still clean them up."""
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

    async def _notify_after(self, delay: float, event: str) -> None:
        """Runs on the ws loop's own thread — the point being that the
        adapter hears from a foreign thread after it has begun shutting
        down."""
        await asyncio.sleep(delay)
        for handler in self._handlers.get(event, []):
            handler()

    async def signal_drop(self) -> None:
        await self._notify(Events.RECONNECTING)

    async def drop_and_recover(self, *, outage: float) -> None:
        await self.signal_drop()
        await asyncio.sleep(outage)
        await self._notify(Events.RECONNECTED)

    async def deliver_frame(self, frame: Any) -> None:
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
        disconnect_error: BaseException | None = None,
        late_signal_delay: float | None = None,
        failures_before_success: int | None = None,
    ) -> None:
        self._connect_error = connect_error
        self._reconnecting_before_error = reconnecting_before_error
        self._disconnect_error = disconnect_error
        self._late_signal_delay = late_signal_delay
        #: `None` means the failure never ends. A number means the outage
        #: recovers on the attempt after that many — `run()` builds exactly
        #: one channel per attempt, so counting constructions counts attempts.
        self._failures_before_success = failures_before_success
        self._built = 0
        self.channels: list[_FakeChannel] = []

    def __call__(self, *, app_id: str, app_secret: str) -> _FakeChannel:
        del app_id, app_secret
        self._built += 1
        connect_error = self._connect_error
        if (
            self._failures_before_success is not None
            and self._built > self._failures_before_success
        ):
            connect_error = None
        channel = _FakeChannel(
            connect_error=connect_error,
            reconnecting_before_error=self._reconnecting_before_error,
            disconnect_error=self._disconnect_error,
            late_signal_delay=self._late_signal_delay,
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
        _inbound("hello", message_id="om_lc_1")
    )
    stop.set()
    await running

    # Written-and-reachable, not "the adapter did not raise": `on_frame`
    # swallows every exception, so the only evidence that the claim actually
    # happened is the row.
    assert await _claimed_event_count(store, long_id, "om_lc_1") == 1


async def _deliver_one_frame(
    scheduler_connections: Callable[[], Awaitable[tuple[FeishuLongConnection, ...]]],
    sdk_channels: Callable[..., _FakeChannels],
    *,
    body: str,
    event_id: str,
) -> None:
    """One real frame, all the way through whatever `_scheduler` wired.

    Through `_long_connections` rather than a hand-built adapter, for the
    same reason `scheduler_connections` exists: the two tests below are
    about what production hands the adapter as its `deliver`, so building
    one here would make them pass regardless of that wiring.
    """
    channels = sdk_channels()
    (connection,) = await scheduler_connections()
    stop = asyncio.Event()
    running = asyncio.create_task(connection.run(stop))
    channel = await channels.connected()
    await channel.deliver_frame(_inbound(body, message_id=event_id))
    stop.set()
    await running


async def _claim_and_run(
    engine: AsyncEngine, binding_id: UUID, channel_event_id: str
) -> tuple[UUID, UUID | None]:
    """The claim this delivery took, and the Run attached to it.

    Found by `(channel_binding_id, channel_event_id)` — the pair
    `claim_delivery` is unique on — rather than by reading the newest row:
    `channel_events` is ordered by `created_at` with no tiebreaker, so a
    positional read names a different row on a different run.
    """
    async with engine.connect() as connection:
        found = await connection.execute(
            text(
                "SELECT id, run_id FROM channel_events"
                " WHERE channel_binding_id = :b AND channel_event_id = :e"
            ),
            {"b": binding_id, "e": channel_event_id},
        )
        claim_id, run_id = found.one()
    return claim_id, run_id


async def test_a_frame_over_the_long_connection_becomes_a_run(
    engine: AsyncEngine,
    seeded_bindings_of_both_transports: tuple[UUID, UUID],
    scheduler_connections: Callable[[], Awaitable[tuple[FeishuLongConnection, ...]]],
    sdk_channels: Callable[..., _FakeChannels],
) -> None:
    """判据不是「deliver 返回了 Accepted」，是「有人够得着」：`runs` 里多了
    一行，且 `channel_events.run_id` 指向它。少任何一半，发消息的人都等不到
    回复——出站扫描认的就是 `run_id` 那一列。

    第三条断言（Run 的 Session 就是这个绑定给这位发件人记的那个）是防止
    「有一个 Run」被一个属于别人的 Run 满足：`channel_conversations` 是
    下一条消息续上同一段对话的依据，也是回复找得到收件人的依据。
    """
    _webhook_id, long_id = seeded_bindings_of_both_transports

    await _deliver_one_frame(
        scheduler_connections, sdk_channels, body="上周几单？", event_id="om_lc_run_1"
    )

    _claim_id, run_id = await _claim_and_run(engine, long_id, "om_lc_run_1")
    assert run_id is not None
    async with engine.connect() as connection:
        found = await connection.execute(
            text("SELECT session_id FROM runs WHERE id = :r"), {"r": run_id}
        )
        session_id = found.scalar_one()
        conversation = await connection.execute(
            text(
                "SELECT session_id FROM channel_conversations"
                " WHERE channel_binding_id = :b AND external_user_id = 'ou_zhang'"
            ),
            {"b": long_id},
        )
    assert conversation.scalar_one() == session_id


class _RecordingSender:
    """A `ChannelSenders`/`ChannelSender` that records instead of calling
    Feishu — the same stand-in `test_reply_dispatch` uses, kept minimal
    here because this file asserts only that something was sent, and to
    whom, not what the cards look like.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def __call__(self, workspace_id: UUID, /) -> "_RecordingSender":
        del workspace_id
        return self

    def after_opening(self) -> list[dict[str, str]]:
        """Everything but the card every delivery opens with.

        Identified by the `:c` delivery-key suffix, as `test_reply_dispatch`
        does: without this, "something was sent" is satisfied by the opening
        card alone and the answer itself could still be going nowhere.
        """
        return [entry for entry in self.sent if not entry["delivery_key"].endswith(":c")]

    def opening(self) -> dict[str, str]:
        """The card the delivery opened with — which is where the addressing
        is. The answer arrives as an *update* to this card, and an update is
        addressed by `message_id`, so `open_id` and the app credentials are
        only checkable here. Same split `test_reply_dispatch` asserts across.
        """
        opened = [entry for entry in self.sent if entry["delivery_key"].endswith(":c")]
        assert opened, "no opening card was sent"
        return opened[0]

    async def send_text(
        self,
        *,
        app_id: str,
        app_secret: str,
        open_id: str,
        text: str,
        delivery_key: str | None = None,
    ) -> None:
        self.sent.append(
            {
                "app_id": app_id,
                "app_secret": app_secret,
                "open_id": open_id,
                "text": text,
                "delivery_key": delivery_key or "",
            }
        )

    async def send_card(
        self,
        *,
        app_id: str,
        app_secret: str,
        open_id: str,
        card: dict[str, Any],
        delivery_key: str | None = None,
    ) -> str | None:
        self.sent.append(
            {
                "app_id": app_id,
                "app_secret": app_secret,
                "open_id": open_id,
                "text": json.dumps(card, ensure_ascii=False),
                "delivery_key": delivery_key or "",
            }
        )
        return "om_card"

    async def update_card(
        self, *, app_id: str, app_secret: str, message_id: str, card: dict[str, Any]
    ) -> None:
        self.sent.append(
            {
                "app_id": app_id,
                "app_secret": app_secret,
                "open_id": "",
                "text": json.dumps(card, ensure_ascii=False),
                "delivery_key": "",
            }
        )


async def _finish(engine: AsyncEngine, run_id: UUID, *, said: str) -> None:
    """What the Worker does at the end of a Run, in one statement — the same
    stand-in `test_reply_dispatch._finish` uses. Driving a real model here
    would test the Worker, which has its own suite."""
    async with engine.begin() as connection:
        row = await connection.execute(
            text("SELECT session_id, workspace_id FROM runs WHERE id = :r"), {"r": run_id}
        )
        session_id, workspace_id = row.one()
        await connection.execute(
            text("UPDATE runs SET status = 'completed', finished_at = now() WHERE id = :r"),
            {"r": run_id},
        )
        await connection.execute(
            text(
                "INSERT INTO session_messages"
                " (id, session_id, workspace_id, sequence, role, content,"
                "  source_run_id, redacted, created_at)"
                " VALUES (gen_random_uuid(), :s, :w,"
                "  (SELECT coalesce(max(sequence), 0) + 1 FROM session_messages"
                "   WHERE session_id = :s), 'assistant', :c, :r, false, now())"
            ),
            {
                "s": session_id,
                "w": workspace_id,
                "c": json.dumps({"parts": [{"type": "text", "text": said}]}),
                "r": run_id,
            },
        )


async def test_the_answer_to_a_long_connection_message_reaches_its_sender(
    engine: AsyncEngine,
    seeded_bindings_of_both_transports: tuple[UUID, UUID],
    scheduler_connections: Callable[[], Awaitable[tuple[FeishuLongConnection, ...]]],
    sdk_channels: Callable[..., _FakeChannels],
) -> None:
    """分支唯一真正的验收，缩到一条测试里：隧道不在了，消息还是被回答。

    出站扫描不认 transport，它认 `channel_events.run_id`——所以这条测试守的
    是「认领和 Run 连上了」这件事对**发消息的人**意味着什么，而不只是那一列
    非空。断言落在 sender 上，因为「平台记录了它回复过」正是这个仓库已经相信
    过五次、而对面什么都没收到的那句话。
    """
    _webhook_id, long_id = seeded_bindings_of_both_transports

    await _deliver_one_frame(
        scheduler_connections, sdk_channels, body="上周几单？", event_id="om_lc_reply_1"
    )
    _claim_id, run_id = await _claim_and_run(engine, long_id, "om_lc_reply_1")
    assert run_id is not None, "no Run to finish: the inbound half never got that far"
    await _finish(engine, run_id, said="上周有 12 单。")

    sender = _RecordingSender()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        dispatched = await ChannelReplyDispatcher(
            store=SqlChannelStore(db),
            resolve_secret=CredentialResolver(SqlSecretStore(db), KEK).resolve,
            senders=sender,
        ).dispatch_once()
        await db.commit()

    assert dispatched >= 1
    answers = sender.after_opening()
    assert len(answers) == 1, sender.sent
    assert "上周有 12 单。" in answers[0]["text"]
    # As the right app, to the right person. Asserted on the opening card
    # because that is where this platform addresses a delivery: the answer
    # goes out as an update to that card, keyed by its `message_id`, so an
    # answer carries no `open_id` of its own. Without these three lines the
    # assertion above is satisfied by an answer sent under the webhook
    # binding's credentials, or to somebody else entirely.
    opening = sender.opening()
    assert opening["open_id"] == "ou_zhang"
    assert opening["app_id"] == "cli_long"
    assert opening["app_secret"] == "app-secret-value"  # noqa: S105


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

    async def never_delivers(binding_id: UUID, envelope: dict[str, Any]) -> None:
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


async def test_a_failed_connect_leaves_no_leaked_channel_and_no_disconnect_row(
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
    # One attempt writes nothing at all. A socket that never came up has not
    # "disconnected" — the word implies an outage with a start instant to
    # measure a later `down_seconds` from — and whether a *failed connect* is
    # worth a row depends on whether it is the first of a run of them, which
    # only the retry loop knows. The two tests below own that half.
    assert await _connection_events(store) == []


async def test_an_outage_that_keeps_failing_is_capped_at_one_audit_row(
    store: SqlChannelBindingStore,
    scheduler_connections: Callable[[], Awaitable[tuple[FeishuLongConnection, ...]]],
    seeded_bindings_of_both_transports: tuple[UUID, UUID],
    sdk_channels: Callable[..., _FakeChannels],
) -> None:
    """一行审计对应一段故障，不是对应一次重试。

    退避封顶 300s、重试永不停止，所以「每轮写一行」在稳态是约 262 行/天/绑定，
    而 `audit_events` 没有任何保留期或清理（`event_retention_hours` 管的是
    `run_events`）。填错一次 app secret，四小时后这个 workspace 的审计页第一页
    就全是 `connect_failed`——那正是 `_supervised_connection` 声称要照顾的那个
    「读控制台的人」再也读不到别的东西的时候。
    """
    channels = sdk_channels(
        connect_error=FeishuChannelError(
            FeishuChannelErrorCode.NOT_CONNECTED, "WebSocket connect failed"
        )
    )
    (connection,) = await scheduler_connections()
    stop = asyncio.Event()

    async def stop_once_it_has_tried_a_few_times() -> None:
        await channels.attempted(ATTEMPTS)
        stop.set()

    await asyncio.gather(
        _supervised_connection(
            connection, stop, first_delay=BACKOFF_SECONDS, max_delay=BACKOFF_SECONDS
        ),
        stop_once_it_has_tried_a_few_times(),
    )

    assert channels.connects >= ATTEMPTS, "gave up instead of retrying"
    events = await _connection_events(store)
    # Two rows cap an outage, and this one never ended — so only the left
    # edge exists yet, however many attempts went into it.
    assert [event["kind"] for event in events] == ["connect_failed"]
    assert _only(events, "connect_failed")["result"] == "failed"


async def test_an_outage_that_ends_is_closed_out_with_how_long_it_lasted(
    store: SqlChannelBindingStore,
    scheduler_connections: Callable[[], Awaitable[tuple[FeishuLongConnection, ...]]],
    seeded_bindings_of_both_transports: tuple[UUID, UUID],
    sdk_channels: Callable[..., _FakeChannels],
) -> None:
    """封顶两行的第二行不是可选的。

    §19.2 的补投检查要靠这一对行读出停机窗口。只写 `connect_failed` 就封顶，
    等于把「淹掉的审计页」换成「量不出任何东西的审计页」——一个只有左边界的
    窗口什么也说明不了。
    """
    channels = sdk_channels(
        connect_error=FeishuChannelError(
            FeishuChannelErrorCode.NOT_CONNECTED, "WebSocket connect failed"
        ),
        failures_before_success=2,
    )
    (connection,) = await scheduler_connections()
    stop = asyncio.Event()
    supervised = asyncio.create_task(
        _supervised_connection(
            connection, stop, first_delay=BACKOFF_SECONDS, max_delay=BACKOFF_SECONDS
        )
    )

    await channels.connected()
    stop.set()
    await supervised

    events = await _connection_events(store)
    assert sorted(event["kind"] for event in events) == ["connect_failed", "reconnected"]
    recovered = _only(events, "reconnected")
    assert recovered["result"] == "succeeded"
    # Measured across the whole run of failures, not from the attempt that
    # finally worked: two backoffs had to elapse before it.
    assert recovered["context"]["down_seconds"] >= BACKOFF_SECONDS


async def test_a_retry_on_the_same_instance_does_not_inherit_the_last_rounds_state(
    store: SqlChannelBindingStore,
    scheduler_connections: Callable[[], Awaitable[tuple[FeishuLongConnection, ...]]],
    seeded_bindings_of_both_transports: tuple[UUID, UUID],
    sdk_channels: Callable[..., _FakeChannels],
) -> None:
    """`_supervised_connection` 在**同一个实例**上循环调 `run()`。

    所以一次 `run()` 抛出并**不**结束这个实例的 watch，而「连上过」这个标志
    如果跨轮存活，下一轮压根没起来的 socket 就会被 `_on_reconnecting` 当成
    「掉线」记一行——一句关于一个从未存在过的连接的谎。现实中最可能触发它的
    是 `finally` 里的 `channel.disconnect()` 自己抛，所以这里就这么造。
    """
    up_then_broken = sdk_channels(disconnect_error=RuntimeError("socket teardown failed"))
    (connection,) = await scheduler_connections()
    stop = asyncio.Event()
    running = asyncio.create_task(connection.run(stop))

    await up_then_broken.connected()
    stop.set()
    with pytest.raises(RuntimeError):
        await running

    sdk_channels(
        connect_error=FeishuChannelError(
            FeishuChannelErrorCode.NOT_CONNECTED, "WebSocket connect failed"
        ),
        reconnecting_before_error=True,
    )
    with pytest.raises(FeishuChannelError):
        await connection.run(asyncio.Event())

    assert await _connection_events(store) == []


async def test_a_recording_that_starts_while_run_is_draining_is_still_written(
    store: SqlChannelBindingStore,
    sdk_channels: Callable[..., _FakeChannels],
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
) -> None:
    """`run()` 的 `finally` 说它等「还在途中的任何一个」写完。

    一次性快照等不到在 `gather` 期间才进来的那个：SDK 的 ws loop 是 import 期
    建的模块级 loop，`_stop_private_ws_client` 只是 best-effort 停它、不 join，
    所以 `disconnect()` 返回之后仍可能有一次回调落进来。它落进来时快照已经取
    完了，于是 `run()` 带着一个在途的事务返回，`asyncio.run` 拆 loop 把那个
    task 当 pending 销毁——事务回滚，审计行没了，只留下一行谁也定位不到的
    "Task was destroyed but it is pending"。
    """
    binding_id = await _seed_binding(
        engine, workspace_id, published_agent, transport="long_connection", app_id="cli_late"
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    written = _connection_event_recorder(sessions, UUID(workspace_id))
    order: list[str] = []

    async def slow_record(binding_id: UUID, kind: str, down_seconds: float | None) -> None:
        await asyncio.sleep(SLOW_RECORD_SECONDS)
        await written(binding_id, kind, down_seconds)
        order.append(kind)

    async def never_delivers(binding_id: UUID, envelope: dict[str, Any]) -> None:
        raise AssertionError("this test never sends a frame")

    connection = FeishuLongConnection(
        LongConnectionBinding(binding_id=binding_id, app_id="cli_late", app_secret="s"),
        never_delivers,
        record=slow_record,
    )
    channels = sdk_channels(late_signal_delay=LATE_SIGNAL_SECONDS)
    stop = asyncio.Event()
    running = asyncio.create_task(connection.run(stop))

    channel = await channels.connected()
    await channel.signal_drop()
    stop.set()
    await running
    order.append("returned")

    try:
        # Ordering, not just presence: reading the rows takes a moment of its
        # own, and a `run()` that returned early would sometimes find the row
        # arriving during that moment and pass for the wrong reason.
        assert order == ["disconnected", "reconnected", "returned"]
        events = await _connection_events(store)
        assert _only(events, "reconnected")["result"] == "succeeded"
    finally:
        channel.close_loops()


async def test_a_binding_whose_secret_cannot_be_resolved_is_skipped(
    scheduler_connections: Callable[[], Awaitable[tuple[FeishuLongConnection, ...]]],
    binding_with_bad_credentials: UUID,
) -> None:
    del binding_with_bad_credentials  # the row this test expects to be dropped

    started = await scheduler_connections()

    assert started == ()


#: 两条跳过原因各自的特征词。放在一起，是因为要求不是「每行都带了原因」——
#: 同一句话写在两条分支上也能让那种断言全绿——而是**读的人分得出是哪一种**。
#: 所以每条测试都断言自己那个词在、另一个词不在。
NO_CREDENTIALS_PHRASE = "app credentials"
UNRESOLVABLE_REF_PHRASE = "app secret"


async def test_a_binding_with_no_app_credentials_says_so_where_the_console_reads(
    store: SqlChannelBindingStore,
    scheduler_connections: Callable[[], Awaitable[tuple[FeishuLongConnection, ...]]],
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
) -> None:
    """`transport` 是长连接但 `app_secret_ref` 是空的，得有人看得见。

    控制台上这个绑定照样显示「长连接」——这里不改它自己那一行——所以缺了
    凭据这件事如果只落在容器日志里，切开关的那个人就永远不知道消息为什么
    不来。判据是审计表里真有这一行，不是「日志打了」。
    """
    binding_id = await _seed_binding(
        engine, workspace_id, published_agent, transport="long_connection"
    )

    started = await scheduler_connections()

    assert started == ()
    refused = _only(await _connection_events(store), "not_started")
    assert refused["binding"] == binding_id
    assert refused["result"] == "failed"
    reason = refused["context"]["reason"]
    assert NO_CREDENTIALS_PHRASE in reason
    assert UNRESOLVABLE_REF_PHRASE not in reason


async def test_a_binding_whose_secret_was_disabled_says_so_where_the_console_reads(
    store: SqlChannelBindingStore,
    scheduler_connections: Callable[[], Awaitable[tuple[FeishuLongConnection, ...]]],
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
) -> None:
    """凭据齐备，但引用的 Secret 在密钥页被禁用了。

    这是控制台上点得到的那条路：切成长连接时校验通过（200），过几天有人把
    那个 secret 禁掉，下次重启 scheduler 这个绑定就静悄悄地不起了。原因必须
    和「压根没配凭据」分得开——两者的修法不一样：一个是去填凭据，一个是去
    把这个 Secret 重新启用。
    """
    secret_id = await _stored_secret(
        engine, workspace_id, "disabled-app-secret", status="disabled"
    )
    binding_id = await _seed_binding(
        engine,
        workspace_id,
        published_agent,
        transport="long_connection",
        app_id="cli_disabled",
        app_secret_ref=str(secret_id),
    )

    started = await scheduler_connections()

    assert started == ()
    refused = _only(await _connection_events(store), "not_started")
    assert refused["binding"] == binding_id
    assert refused["result"] == "failed"
    reason = refused["context"]["reason"]
    assert UNRESOLVABLE_REF_PHRASE in reason
    assert NO_CREDENTIALS_PHRASE not in reason
    # 光说「解析不出来」不够——要说是哪一个 Secret，读的人才知道去启用哪个。
    assert refused["context"]["app_secret_ref"] == str(secret_id)


async def _never_delivers(binding_id: UUID, envelope: dict[str, Any]) -> None:
    """下面这几条测试一帧都不发——`deliver` 被叫到就是缺陷本身。"""
    raise AssertionError(f"no frame should have reached deliver: {binding_id} {envelope}")


@pytest.fixture
async def two_long_connection_bindings(
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    client: TestClient,
    scope: dict[str, str],
) -> tuple[UUID, UUID]:
    """两个都**能用**的长连接绑定：凭据齐全、secret 解得开。

    两个都能用是关键。任何一个因为凭据问题被 `_long_connections` 的
    `continue` 挡掉，「只起一个」这条断言就会因为错误的理由变绿。
    """
    first_secret = await _stored_secret(engine, workspace_id, "first-app-secret")
    first = await _seed_binding(
        engine,
        workspace_id,
        published_agent,
        transport="long_connection",
        app_id="cli_first",
        app_secret_ref=str(first_secret),
    )
    second_agent = _second_agent(client, scope)
    second_secret = await _stored_secret(engine, workspace_id, "second-app-secret")
    second = await _seed_binding(
        engine,
        workspace_id,
        second_agent,
        transport="long_connection",
        app_id="cli_second",
        app_secret_ref=str(second_secret),
    )
    return first, second


async def test_only_one_long_connection_binding_can_hold_the_processs_single_sdk_loop(
    store: SqlChannelBindingStore,
    scheduler_connections: Callable[[], Awaitable[tuple[FeishuLongConnection, ...]]],
    two_long_connection_bindings: tuple[UUID, UUID],
) -> None:
    """一个进程里带不动两个长连接，所以第二个必须**被人看见**地不启动。

    `lark_oapi/ws/client.py:31-35` 在 import 时建了一个**模块级** loop，
    `Client.start()` 在它上面 `run_until_complete`，而 `FeishuChannel.stop()`
    经 `_stop_private_ws_client`（`channel/channel.py:853-876`）
    `call_soon_threadsafe(ws_loop.stop)` 停的也是那一个。两个绑定同进程：第二个
    起不来，第一个的 `disconnect()` 又会把第二个活着的 socket 所在的 loop 停掉。

    默默少起一个正是这个项目的签名缺陷——用户在控制台把第二个绑定切成长连接、
    看见它显示「长连接」、消息永远不来，而后端测试全绿。所以判据不是「返回了
    一个连接」，是「另一个绑定在审计页上留下了一行说明为什么没起」。
    """
    first, second = two_long_connection_bindings
    held, crowded_out = sorted((first, second), key=str)

    started = await scheduler_connections()

    assert [connection.binding_id for connection in started] == [held]
    events = await _connection_events(store)
    refused = _only(events, "not_started")
    assert refused["binding"] == crowded_out
    assert refused["result"] == "failed"
    # 这一行要能自己解释「为什么」，否则它和没写一样。
    assert refused["context"]["holding_binding_id"] == str(held)
    assert refused["context"]["reason"]


async def test_an_outage_whose_left_edge_never_landed_is_not_closed_out(
    store: SqlChannelBindingStore,
    sdk_channels: Callable[..., _FakeChannels],
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
) -> None:
    """`connect_failed` 写失败了，就没有故障段可关。

    `record_connect_failed` 把每一个异常都吃掉（它必须吃——审计写不进去不该
    再把 socket 带下去），所以「叫过它」和「那一行真的在表里」是两件事。
    重试循环把前者当成后者，于是数据库里只剩一条没有左边界的 `reconnected`：
    §19.2 的补投检查按一对行读停机窗口，只有右边界的窗口量不出任何东西，
    而审计页上它还是一行绿色的「succeeded」。
    """
    binding_id = await _seed_binding(
        engine,
        workspace_id,
        published_agent,
        transport="long_connection",
        app_id="cli_halfopen",
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    written = _connection_event_recorder(sessions, UUID(workspace_id))

    async def audit_that_drops_the_left_edge(
        binding_id: UUID, kind: str, down_seconds: float | None
    ) -> None:
        if kind == "connect_failed":
            raise RuntimeError("audit insert failed")
        await written(binding_id, kind, down_seconds)

    connection = FeishuLongConnection(
        LongConnectionBinding(binding_id=binding_id, app_id="cli_halfopen", app_secret="s"),
        _never_delivers,
        record=audit_that_drops_the_left_edge,
    )
    channels = sdk_channels(
        connect_error=FeishuChannelError(
            FeishuChannelErrorCode.NOT_CONNECTED, "WebSocket connect failed"
        ),
        failures_before_success=1,
    )
    stop = asyncio.Event()
    supervised = asyncio.create_task(
        _supervised_connection(
            connection, stop, first_delay=BACKOFF_SECONDS, max_delay=BACKOFF_SECONDS
        )
    )

    await channels.connected()
    stop.set()
    await supervised

    events = await _connection_events(store)
    assert events == [], "closed out an outage whose left edge was never written"


async def test_a_disconnect_that_raises_still_waits_for_the_recordings_in_flight(
    store: SqlChannelBindingStore,
    sdk_channels: Callable[..., _FakeChannels],
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
) -> None:
    """`disconnect()` 抛出来，不该把 drain 整段跳过。

    `run()` 的 `finally` 是两句顺序语句，第一句抛就没有第二句。真实的最后一轮
    （`stop` 已置）上这正是 `_drain_recordings` 存在的理由消失的地方：在飞的
    审计写没人等，`asyncio.run` 拆 loop 时把它当 pending 销毁，事务回滚，
    只留下一行谁也定位不到的 "Task was destroyed but it is pending"。

    判据是**顺序**，不只是「行最后出现了」：测试自己的 loop 在 `run()` 抛完
    之后还活着，那一行迟早会写进去——早返回的 `run()` 会因为断言语句本身花的
    那点时间而蒙混过关。
    """
    binding_id = await _seed_binding(
        engine,
        workspace_id,
        published_agent,
        transport="long_connection",
        app_id="cli_teardown",
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    written = _connection_event_recorder(sessions, UUID(workspace_id))
    order: list[str] = []

    async def slow_record(binding_id: UUID, kind: str, down_seconds: float | None) -> None:
        await asyncio.sleep(SLOW_RECORD_SECONDS)
        await written(binding_id, kind, down_seconds)
        order.append(kind)

    connection = FeishuLongConnection(
        LongConnectionBinding(binding_id=binding_id, app_id="cli_teardown", app_secret="s"),
        _never_delivers,
        record=slow_record,
    )
    channels = sdk_channels(disconnect_error=RuntimeError("socket teardown failed"))
    stop = asyncio.Event()
    running = asyncio.create_task(connection.run(stop))

    channel = await channels.connected()
    await channel.signal_drop()
    stop.set()
    with pytest.raises(RuntimeError):
        await running
    order.append("raised")

    try:
        assert order == ["disconnected", "raised"]
        assert _only(await _connection_events(store), "disconnected")["result"] == "failed"
    finally:
        channel.close_loops()


async def test_a_drain_that_cannot_finish_gives_up_instead_of_hanging_the_shutdown(
    store: SqlChannelBindingStore,
    sdk_channels: Callable[..., _FakeChannels],
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """等不到就放弃，并且说清楚放弃了几条。

    让 drain 停下来的不是 `disconnect()`（`_stop_private_ws_client` 只是
    best-effort 停那个模块级 ws loop，从不 join 它），而是那个 loop 最终自己
    停下来。一个抖动的 socket 在此期间可以一直往里塞回调，于是 `run()`、
    进而整个 scheduler 的关闭被无限期吊住——没有任何日志说明它卡在哪。

    截止时间换来的是一次**有界**的丢失，代价必须被说出来：这里断言那一行
    审计**没有**写进去，而不是假装 drain 还是等到了。
    """
    monkeypatch.setattr(feishu_long_connection, "DRAIN_TIMEOUT_SECONDS", 0.05)
    binding_id = await _seed_binding(
        engine,
        workspace_id,
        published_agent,
        transport="long_connection",
        app_id="cli_wedged",
    )
    released = asyncio.Event()

    async def never_finishes(
        binding_id: UUID, kind: str, down_seconds: float | None
    ) -> None:
        del binding_id, kind, down_seconds
        await released.wait()

    connection = FeishuLongConnection(
        LongConnectionBinding(binding_id=binding_id, app_id="cli_wedged", app_secret="s"),
        _never_delivers,
        record=never_finishes,
    )
    channels = sdk_channels()
    stop = asyncio.Event()
    running = asyncio.create_task(connection.run(stop))

    channel = await channels.connected()
    await channel.signal_drop()
    stop.set()
    try:
        with caplog.at_level("ERROR"):
            await asyncio.wait_for(running, timeout=5.0)
        assert "gave up waiting" in caplog.text
        assert await _connection_events(store) == []
    finally:
        released.set()
        channel.close_loops()


async def test_a_real_sdk_frame_becomes_a_run(
    engine: AsyncEngine,
    seeded_bindings_of_both_transports: tuple[UUID, UUID],
    scheduler_connections: Callable[[], Awaitable[tuple[FeishuLongConnection, ...]]],
    sdk_channels: Callable[..., _FakeChannels],
) -> None:
    """真机走查抓到的那个缺陷，钉在这里。

    上面每一条长连接测试递给 `deliver` 的都是**手搭的 webhook 信封**，
    于是 `_envelope_of` 从来没有被真实帧走过一遍。生产上第一条真实消息
    的结果是 `no event id in either schema version`：SDK 的
    `InboundMessage.raw` 是**消息对象**，不是事件信封，事件 id 根本没进去。

    这条测试递的是 SDK 自己的 `InboundMessage`，所以它证明的是「SDK 实际
    给什么」，不是「我们以为 SDK 会给什么」。
    """
    _webhook_id, long_id = seeded_bindings_of_both_transports

    channels = sdk_channels()
    (connection,) = await scheduler_connections()
    stop = asyncio.Event()
    running = asyncio.create_task(connection.run(stop))
    channel = await channels.connected()
    await channel.deliver_frame(_inbound("上周几单？", message_id="om_real_1"))
    stop.set()
    await running

    _claim_id, run_id = await _claim_and_run(engine, long_id, "om_real_1")
    assert run_id is not None


async def test_a_socket_that_never_comes_back_does_not_park_the_watch_forever(
    scheduler_connections: Callable[[], Awaitable[tuple[FeishuLongConnection, ...]]],
    sdk_channels: Callable[..., _FakeChannels],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """建连成功之后掉线、SDK 再也没连回来——这个 watch 必须自己放弃。

    在此之前 `run()` 停在 `await stop.wait()` 上，而 `stop` 只有进程关闭才会置。
    SDK 报了 `RECONNECTING` 却再没报 `RECONNECTED`，就没有任何东西会叫醒它：
    进程抱着一根死 socket 一直等，消息不来，日志里也不会再有新行。
    验收记录把这一条明写成缺口（`2026-08-31-feishu-long-connection.md`
    「不声称建连成功之后掉线会被自动恢复」），这条测试是来把它补掉的。

    判据是 `run()` **返回了**，不是 `alive` 变成了 False：一个把标志翻过来却
    照样停在那儿等的实现也会让后者为真，而那正是这条测试要排除的东西。
    """
    monkeypatch.setattr(feishu_long_connection, "OUTAGE_GIVE_UP_SECONDS", 0.2)
    monkeypatch.setattr(feishu_long_connection, "LIVENESS_POLL_SECONDS", 0.05)

    channels = sdk_channels()
    (connection,) = await scheduler_connections()
    stop = asyncio.Event()
    running = asyncio.create_task(connection.run(stop))
    channel = await channels.connected()

    # 连上了，然后掉线，然后再也没有 RECONNECTED。
    await channel.signal_drop()

    # `stop` 一次都没有被置——这一点是这条测试的全部意义。
    await asyncio.wait_for(running, timeout=5)
    assert not stop.is_set()
    assert connection.alive is False
