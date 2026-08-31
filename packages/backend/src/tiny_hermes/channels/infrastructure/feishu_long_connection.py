"""§19.2's default transport for a private deployment: a WebSocket long
connection instead of a public webhook address that a temporary tunnel has
to keep alive.

`lark_oapi.channel.FeishuChannel` owns the socket: it authenticates each
frame, keeps the link alive, and reconnects on its own. What this module
owns is everything after that hand-off. It knows nothing about
`FeishuWebhookService` — only the `DeliverFrame` protocol, whose shape
mirrors `webhook_service.accept_verified` — so the two transports share
exactly one place that claims a delivery, and this adapter can be tested
without a database.

**None of the SDK's callbacks arrive on the loop that hosts this adapter.**
`lark_oapi/ws/client.py` builds a module-level loop at import time and runs
the socket on it; `FeishuChannel` spawns a second one on its
`lark-channel-bg` thread and pushes message callbacks there with
`run_coroutine_threadsafe`. A coroutine started on either of those cannot
touch the SQLAlchemy engine the scheduler built on *its* loop — asyncpg
raises `RuntimeError: ... got Future ... attached to a different loop`, and
both callback paths here catch `Exception`, so the failure would show up as
one log line and no row. That is why `run()` records the loop it was called
on and every database-touching coroutine is handed back to it.
"""

import asyncio
import logging
import threading
import time
from collections.abc import Coroutine
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, cast
from uuid import UUID

from lark_oapi.channel import (  # pyright: ignore[reportMissingTypeStubs]
    Events,
    FeishuChannel,
)

from tiny_hermes.channels.application.webhook_service import Claimed, Unreadable
from tiny_hermes.channels.domain._json import object_at, string_at

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


@dataclass(frozen=True)
class LongConnectionBinding:
    """The one binding a `FeishuLongConnection` speaks for.

    A single `run()` covers one tenant's app credentials; a deployment with
    several long-connection bindings runs one of these per binding rather
    than teaching this adapter to multiplex, which keeps a crash in one
    tenant's socket from taking another tenant's connection down with it.
    """

    binding_id: UUID
    app_id: str
    app_secret: str


class DeliverFrame(Protocol):
    """Only the question this adapter is allowed to ask of the rest of the
    platform. Narrow on purpose, the same reasoning as
    `webhook_service.DeliveryClaims`: a wider port would let a long-connection
    frame reach past this seam into whatever `FeishuWebhookService` needs,
    which is exactly the coupling both transports converging on
    `accept_verified` exists to avoid.
    """

    async def __call__(
        self, binding_id: UUID, envelope: dict[str, Any]
    ) -> Claimed | Unreadable: ...


class RecordConnectionEvent(Protocol):
    """Only the question this adapter is allowed to ask about persistence for
    a connection's own lifecycle — deliberately the same narrowing as
    `DeliverFrame`.

    Three kinds, and they are not interchangeable:

    - `"connect_failed"` — this process could not get the socket up at all.
    - `"disconnected"` — a socket that *was* up went down. Only ever
      recorded after `run()` saw the connect succeed, because §19.2's
      redelivery check reads the pair of rows as an outage window and a
      window whose left edge is "we were never up" measures nothing.
    - `"reconnected"` — that outage ended.

    `down_seconds` carries a real, non-negative number only on
    `"reconnected"`. It is `None` on the other two kinds, and also on a
    `"reconnected"` this instance cannot measure (see `_record_reconnected`)
    — an implementation must record `None` as a value a reader can see,
    not as an absent field: "the outage lasted an unknown time" and "this
    row carries nothing" are different answers to §19.2's question.
    """

    async def __call__(
        self, binding_id: UUID, kind: str, down_seconds: float | None
    ) -> None: ...


def _envelope_of(frame: Any) -> dict[str, Any]:
    """The SDK's `InboundMessage.raw` is Feishu's own event JSON — the same
    shape `event_from_envelope` already reads off the decrypted webhook body
    (see `doc/channel/reference.md`'s Message Model: "`raw` — Original event
    payload"). Both transports converge on one reader because there is one
    envelope shape to read, not two.

    Validates rather than blindly `cast()`ing: `frame.raw` not being a
    `dict` is exactly the "frame this build cannot read" case `on_frame`
    exists to survive, and `cast()` never raises — it would let a non-dict
    through unnoticed, and every reader downstream trusts the return type
    here rather than checking it again. The failure has to happen in this
    one place or it happens somewhere less obvious later.
    """
    raw = frame.raw
    if not isinstance(raw, dict):
        raise TypeError(f"frame.raw is {type(raw).__name__}, not a dict")
    return cast(dict[str, Any], raw)


def _event_id_of(envelope: Any) -> str:
    """Best-effort id for the one log line `on_frame` writes on failure.

    Takes `Any`, not `dict[str, Any]`: this function's entire job is to be
    callable from a failure path without becoming a second way for that
    path to fail, so it re-checks its input rather than trusting a caller
    who is, by construction, already handling one exception. An earlier
    version trusted the caller and typed this `dict[str, Any]`; called with
    whatever `_envelope_of` had merely `cast()` a non-dict into, it raised
    `AttributeError` out of `object_at`'s own `.get()` call — while it was
    still an argument being evaluated for `logger.exception(...)`, so the
    log call itself never ran and the `AttributeError` replaced the
    original failure on its way out of `on_frame`.

    Mirrors `event_from_envelope`'s two schema versions (`header.event_id`
    for v2, top-level `uuid` for v1) using the same `object_at`/`string_at`
    readers that function uses, rather than a second hand-rolled pair of
    `isinstance` checks — this is not a claim that the envelope is
    well-formed, only an attempt to name it so a failure can be found later.
    A malformed envelope that carries neither is exactly the case worth
    being able to search for, so it gets a literal placeholder rather than a
    log line built with no event id in it at all.
    """
    if not isinstance(envelope, dict):
        return "<no event id>"
    envelope = cast("dict[str, Any]", envelope)
    header = object_at(envelope, "header")
    event_id = string_at(header, "event_id") if header else None
    if event_id is not None:
        return event_id
    top_level = string_at(envelope, "uuid")
    if top_level is not None:
        return top_level
    return "<no event id>"


class FeishuLongConnection:
    def __init__(
        self,
        binding: LongConnectionBinding,
        deliver: DeliverFrame,
        *,
        record: RecordConnectionEvent | None = None,
    ) -> None:
        self._binding = binding
        self._deliver = deliver
        self._record = record
        #: Set while the socket is down, cleared on reconnect — the only
        #: thing this timestamp is for is computing `down_seconds` when the
        #: matching `"reconnected"` event is recorded. `None` otherwise, so
        #: `_record_reconnected` can tell "was down" from "was already up"
        #: (the SDK could in principle fire `reconnected` without a prior
        #: `reconnecting` on this instance's watch — nothing in its source
        #: rules that out — and manufacturing a duration for an outage this
        #: adapter never saw the start of would be worse than recording
        #: none).
        self._down_since: float | None = None
        #: The loop `run()` was called on — the scheduler's. Everything that
        #: touches the database has to run there and nowhere else; see this
        #: module's docstring for which foreign loops the SDK calls back on.
        #: `None` until `run()` sets it, which is also before any handler is
        #: registered, so a callback can never find it unset.
        self._loop: asyncio.AbstractEventLoop | None = None
        #: Handlers registered on `Events.RECONNECTING`/`RECONNECTED` are
        #: called synchronously with no arguments (see `run`'s docstring on
        #: `_on_reconnecting`) — they cannot themselves be `async def` and
        #: awaited, so the actual recording is submitted to `_loop` and left
        #: to run there. Held here so `run`'s `finally` can wait for whatever
        #: is still in flight instead of abandoning it mid-write when the
        #: socket closes. `concurrent.futures.Future`, not `asyncio.Task`,
        #: because the thread that adds one is not the thread that runs it.
        self._background: set[Future[None]] = set()
        #: `_background` is mutated from the SDK's threads (adding) and from
        #: the scheduler loop (the done-callback discarding, and `run`'s
        #: `finally` taking a snapshot).
        self._background_lock = threading.Lock()
        #: Set once `connect_until_ready` has returned, so the recording
        #: paths can tell "a live socket dropped" from "this never came up".
        #: Never cleared: the SDK reconnects underneath us, and `run()`'s
        #: only exit is `stop` or an exception, both of which end this
        #: instance's watch entirely.
        self._connected = False
        #: `on_frame` never clears this on a delivery failure — that is the
        #: one guarantee this attribute carries right now. Nothing else in
        #: this adapter writes to it either, so it does not yet reflect
        #: whether the underlying socket is actually still open; hooking it
        #: up to the SDK's own `reconnecting`/`error` events is for whoever
        #: hosts this in the scheduler process to decide, not this adapter.
        self.alive = True

    @property
    def binding_id(self) -> UUID:
        """Exposed read-only so the process hosting this adapter can log and
        retry per-binding without reaching into a private attribute."""
        return self._binding.binding_id

    async def on_frame(self, frame: Any) -> None:
        """把一帧交给两种 transport 共用的那一半。

        异常在这里被吃掉而不是冒泡：一条读不懂或处理失败的消息若把连接带下去，
        之后所有消息都收不到，代价远大于丢这一条。日志是这条路径上唯一的痕迹，
        所以它必须带上 `binding_id` 和事件 id。

        `_envelope_of(frame)` 本身可能失败（`frame.raw` 缺失或不是
        `dict`）——它会抛，不会悄悄放一个坏值过去，所以这种连信封都读不出来
        的帧在这里就被挡住，日志写一个占位符，`deliver` 从不会被叫到。

        信封一旦转换成功，事件 id 在调用 `deliver` **之前**就取好，而不是等
        `deliver` 炸了之后在 `except` 里现取——`_event_id_of` 自己保证不会
        抛（它对输入类型做了防御性检查，不假设调用者已经保证过什么），但
        提前取还有一个理由：`except` 块里唯一要做的事就是把已经算好的
        `binding_id` 和 `event_id` 写进日志，不再有第二个能失败的调用夹在
        `logger.exception(...)` 的参数求值里，把原本要报的异常顶替掉。

        这个方法由 SDK 在 `lark-channel-bg` 那个 loop 上 `await`（见
        `channel.py` 的 `_invoke`），所以 `deliver` 必须送回 scheduler 的
        loop，而且必须**等**——去重和背压都在 `deliver` 那一半，不等就等于
        没有。
        """
        try:
            envelope = _envelope_of(frame)
        except Exception:
            logger.exception(
                "long connection frame not handled binding=%s event=<unreadable frame>",
                self._binding.binding_id,
            )
            return

        event_id = _event_id_of(envelope)
        try:
            await self._on_scheduler_loop(
                self._deliver(self._binding.binding_id, envelope)
            )
        except Exception:
            logger.exception(
                "long connection frame not handled binding=%s event=%s",
                self._binding.binding_id,
                event_id,
            )

    async def _on_scheduler_loop(self, coro: Coroutine[Any, Any, _T]) -> _T:
        """Runs `coro` on the loop `run()` was called on, and waits for it.

        Waiting, not fire-and-forget, is the point on the frame path: the
        SDK awaits `on_frame` so that a slow claim slows the socket down,
        and `deliver` is where deduplication happens — a caller that does
        not wait has neither.

        Falls back to a plain `await` when no loop was captured. `on_frame`
        is reachable without `run()` (that is how the transport-dedup test
        drives a real frame through this adapter), and there the caller's
        own loop is the only one in play.
        """
        loop = self._loop
        if loop is None:
            return await coro
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop))

    async def _record_disconnected(self) -> None:
        """Marks the outage's start and writes the `"disconnected"` event.

        Recorded with `down_seconds=None` rather than waiting to write a
        single row once the duration is known: a socket that never
        reconnects — the process is killed, the tenant revokes the app —
        would otherwise leave no trace at all that anything went down.
        """
        self._down_since = time.monotonic()
        if self._record is None:
            return
        try:
            await self._record(self._binding.binding_id, "disconnected", None)
        except Exception:
            logger.exception(
                "failed to record disconnect binding=%s", self._binding.binding_id
            )

    async def _record_reconnected(self) -> None:
        """Closes out the outage `_record_disconnected` opened.

        `down_seconds` is only meaningful when this instance actually saw
        the outage start (`_down_since` set); `max(0.0, ...)` guards against
        a negative reading from `time.monotonic()` behaving unexpectedly
        across a suspend/resume, since a negative duration would be a worse
        answer for §19.2's redelivery check than a slightly-off positive one.
        """
        down_since, self._down_since = self._down_since, None
        down_seconds = None if down_since is None else max(0.0, time.monotonic() - down_since)
        if self._record is None:
            return
        try:
            await self._record(self._binding.binding_id, "reconnected", down_seconds)
        except Exception:
            logger.exception(
                "failed to record reconnect binding=%s", self._binding.binding_id
            )

    async def _record_connect_failed(self) -> None:
        """The one trace a connection that never came up leaves behind.

        Without it the most common production outage — the app is not
        enabled for long connection, the secret is wrong, Feishu is
        unreachable at startup — is invisible to §19.2's redelivery check
        and to the console, because `_supervised_connection` retries
        forever and every attempt would otherwise produce only a log line
        inside the scheduler container.
        """
        if self._record is None:
            return
        try:
            await self._record(self._binding.binding_id, "connect_failed", None)
        except Exception:
            logger.exception(
                "failed to record connect failure binding=%s", self._binding.binding_id
            )

    def _fire(self, coro: Coroutine[Any, Any, None]) -> None:
        """Hands a recording to the scheduler's loop from whichever thread
        the SDK called us on.

        `asyncio.ensure_future` was the bug this replaces: it attaches to
        the *running* loop, which on these callbacks is the SDK's, and the
        session opened there dies on the scheduler loop's engine — inside
        an `except Exception` that turns it into a log line.
        """
        loop = self._loop
        if loop is None:
            # Only reachable if a handler runs before `run()` captured the
            # loop, which the registration order in `run()` rules out.
            # Closing the coroutine keeps a real bug from also producing a
            # "never awaited" warning that points somewhere else.
            coro.close()
            logger.error(
                "long connection has no scheduler loop to record on binding=%s",
                self._binding.binding_id,
            )
            return
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        with self._background_lock:
            self._background.add(future)
        future.add_done_callback(self._forget)

    def _forget(self, future: "Future[None]") -> None:
        with self._background_lock:
            self._background.discard(future)

    async def _drain_recordings(self) -> None:
        with self._background_lock:
            pending = tuple(self._background)
        if not pending:
            return
        await asyncio.gather(
            *(asyncio.wrap_future(future) for future in pending), return_exceptions=True
        )

    def _on_reconnecting(self) -> None:
        """`FeishuChannel`'s `_notify_reconnecting` (see
        `lark_oapi/channel/channel.py`) calls a registered handler as `h()` —
        synchronous, zero arguments, and never awaited even if `h` were a
        coroutine function. An `async def` registered directly here would
        build a coroutine object that nothing ever runs, so this stays a
        plain function that schedules the real (async) recording work
        instead of doing it inline.

        The "was it ever up" question is answered here, in the SDK's thread
        at the instant of the signal, rather than inside
        `_record_disconnected`: `ws/client.py`'s `start` calls `_reconnect()`
        when the *first* connect fails too, and by the time a coroutine
        queued for the scheduler loop actually ran, a later successful
        connect could have flipped the flag — recording a "disconnected" for
        a socket that was never up, and dating an outage window from it.
        """
        if not self._connected:
            logger.info(
                "long connection binding=%s signalled reconnecting before it ever"
                " came up; recorded as a failed connect, not as a disconnect",
                self._binding.binding_id,
            )
            return
        self._fire(self._record_disconnected())

    def _on_reconnected(self) -> None:
        """Mirror of `_on_reconnecting` — see its docstring for why this
        cannot itself be `async def`, and why the guard is here."""
        if not self._connected:
            return
        self._fire(self._record_reconnected())

    async def run(self, stop: asyncio.Event) -> None:
        """Own one `FeishuChannel` for the lifetime of this call.

        The first thing it does is remember which loop it is on. That loop
        is the only one the rest of this class may touch a database from —
        see this module's docstring for the two foreign loops the SDK calls
        back on, and `_fire`/`_on_scheduler_loop` for the hand-off.

        `connect_until_ready()` rather than the blocking `connect()`: the
        quickstart recommends it for exactly this shape of caller — one that
        needs control back so it can wait on its own stop signal instead of
        handing the event loop to the SDK.

        **The connect is inside the `try`**, so a failed one still reaches
        `disconnect()`. `FeishuChannel.start` spawns the `lark-channel-bg`
        thread and its loop (and, with bad credentials, a bot-identity retry
        loop on it) *before* the transport can fail, and its
        `_finish_failed_start` resets only `_started` — nothing else joins
        that thread. With `_supervised_connection` retrying forever, a
        connect that raises without `disconnect()` leaks a thread and a loop
        per attempt for as long as the process lives.

        `timeout=30` is spelled out rather than left to the SDK's own
        default (which happens to also be 30) because what happens when it
        expires is worth being explicit about: `_wait_background_start_ready`
        (`lark_oapi/channel/channel.py`) stops the transport and raises
        `FeishuChannelError(NOT_CONNECTED, "Timed out waiting for channel
        transport readiness")`, which propagates out of this `run()` call
        uncaught. Whoever hosts this adapter in the scheduler process is the
        one who decides whether that means retry or give up — this method
        does not swallow it, it only records that it happened.
        """
        self._loop = asyncio.get_running_loop()
        channel = FeishuChannel(
            app_id=self._binding.app_id, app_secret=self._binding.app_secret
        )
        channel.on(Events.MESSAGE, self.on_frame)
        channel.on(Events.RECONNECTING, self._on_reconnecting)
        channel.on(Events.RECONNECTED, self._on_reconnected)
        try:
            await channel.connect_until_ready(timeout=30)
            self._connected = True
            await stop.wait()
        except Exception:
            if not self._connected:
                await self._record_connect_failed()
            raise
        finally:
            await channel.disconnect()
            await self._drain_recordings()
