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
from collections.abc import Awaitable, Callable, Coroutine
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, cast
from uuid import UUID

from lark_oapi.channel import (  # pyright: ignore[reportMissingTypeStubs]
    Events,
    FeishuChannel,
)

from tiny_hermes.channels.domain._json import object_at, string_at

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

#: How long `_drain_recordings` waits for the recordings still in flight
#: before giving up on them and saying so. Read off the module at call time
#: so a test can shorten it.
#:
#: There has to be a bound. Nothing joins the SDK's threads — see
#: `_drain_recordings` — so a socket that keeps signalling can keep queueing
#: recordings for as long as it likes, and an unbounded wait would park
#: `run()`, and with it the scheduler's whole shutdown, with no log line
#: naming where it stopped. 30s is the same order as `run()`'s connect
#: timeout: long enough that a healthy write (one INSERT and a COMMIT) is
#: never cut off, short enough that a process being restarted is not held
#: past what an orchestrator will wait before it sends SIGKILL.
DRAIN_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class LongConnectionBinding:
    """The one binding a `FeishuLongConnection` speaks for.

    A single `run()` covers one tenant's app credentials. This adapter does
    not multiplex, and **one process can only carry one of these**, so there
    is no per-binding isolation here to claim: `lark_oapi/ws/client.py`
    builds its event loop at import time as a module-level global (lines
    31-35) and `Client.start()` drives it with `run_until_complete`, so a
    second binding's `start()` has no loop of its own to run on. Teardown is
    shared the same way — `FeishuChannel.stop()` reaches
    `_stop_private_ws_client` (`lark_oapi/channel/channel.py:853-876`),
    whose last act is `ws_loop.call_soon_threadsafe(ws_loop.stop)` on that
    same module-level loop, so one binding's `disconnect()` stops the loop
    another binding's live socket is running on.

    `api/cli.py`'s `_long_connections` is where that limit is enforced and,
    more importantly, made visible: it starts one binding and writes an
    audit row for every other, rather than letting a tenant switch a second
    binding to `long_connection`, see it succeed, and never receive a
    message.
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

    Returns nothing because this adapter reads nothing: what a delivery
    became — claimed, deduplicated, refused, turned into a Run — is the
    caller's business, and typing the outcome here would drag the
    application's result model across this seam for a value `on_frame`
    only discards.
    """

    async def __call__(self, binding_id: UUID, envelope: dict[str, Any]) -> None: ...


class RecordConnectionEvent(Protocol):
    """Only the question this adapter is allowed to ask about persistence for
    a connection's own lifecycle — deliberately the same narrowing as
    `DeliverFrame`.

    Four kinds, and they are not interchangeable:

    - `"connect_failed"` — this process could not get the socket up at all.
      Written once for a *run* of failed attempts, not once per attempt:
      the retry loop that hosts this adapter decides when a new run of
      failures has begun (see `record_connect_failed`).
    - `"disconnected"` — a socket that *was* up went down. Only ever
      recorded after `run()` saw the connect succeed, because §19.2's
      redelivery check reads the pair of rows as an outage window and a
      window whose left edge is "we were never up" measures nothing.
    - `"reconnected"` — an outage ended. It is meant as the right edge of
      one of the two kinds above, and the callers here only write it when
      they have a left edge to close: `record_recovered` is called only
      after a `connect_failed` row actually landed, and `_record_reconnected`
      only for an instance whose connect succeeded. The one case that still
      gets through is the SDK signalling `RECONNECTED` on a watch that never
      saw a `RECONNECTING` — nothing in its source rules that out — and that
      row is the one carrying `down_seconds=None` (see below).
    - `"not_started"` — a binding configured for the long connection that
      this process is deliberately not connecting, because another binding
      already holds the one socket a process can carry (see
      `LongConnectionBinding`). Written by `api/cli.py` at startup, one row
      per skipped binding, so the limit is something a reader can find
      rather than a connection that silently never happens.

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
    """Rebuilds the webhook envelope `event_from_envelope` reads, from what
    the SDK actually hands a `MESSAGE` handler.

    This function used to claim `InboundMessage.raw` *was* Feishu's event
    JSON and pass it straight through. It is not, and the first real message
    in production failed with `no event id in either schema version`:
    `normalize/pipeline.py` sets `raw=msg`, the **message** object
    (`message_id`, `chat_id`, `message_type`, `content`, `mentions`), while
    the envelope around it stays in the SDK. `channel.py`'s
    `_handle_message_event` reads `data.header.event_id` and passes it to
    the pipeline as its own argument, which `InboundMessage` does not keep —
    so the event id is not merely elsewhere in the frame, it is **gone** by
    the time a handler runs.

    **The dedup key is therefore the message id, not Feishu's event id.**
    Dedup is `(binding_id, channel_event_id)`, and a message id is stable
    across redeliveries of the same message, so it deduplicates what this
    transport can actually be handed twice. What it does not share is a key
    space with the webhook path: a workspace that switches transport
    mid-conversation can have one message claimed once under each. That is
    a transport switch, which already requires a scheduler restart, so it
    is a seam a person crosses deliberately rather than a race.

    Every field is checked here rather than downstream: a frame this build
    cannot read is exactly what `on_frame` exists to survive, and the
    envelope this returns is trusted by every reader after it.
    """
    raw = getattr(frame, "raw", None)
    if not isinstance(raw, dict):
        raise TypeError(f"frame.raw is {type(raw).__name__}, not a dict")
    message = cast(dict[str, Any], raw)

    open_id = getattr(getattr(frame, "sender", None), "open_id", None)
    if not isinstance(open_id, str) or not open_id:
        raise TypeError("frame carries no sender open_id")

    # `frame.id` is `InboundMessage`'s own field; `raw["message_id"]` is the
    # same value copied by the pipeline. Both are read because a frame that
    # reached here with only one of them is still answerable, and refusing
    # it would drop a real person's message over a redundancy.
    message_id = getattr(frame, "id", None) or message.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        raise TypeError("frame carries no message id")

    return {
        "header": {"event_id": message_id},
        "event": {
            "sender": {"sender_id": {"open_id": open_id}},
            "message": message,
        },
    }


def event_id_of(envelope: Any) -> str:
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
        #: Set while the socket is down, cleared on reconnect and at the top
        #: of every `run()` (see `_reset_for_this_run`) — the only thing this
        #: timestamp is for is computing `down_seconds` when the matching
        #: `"reconnected"` event is recorded. `None` otherwise, so
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
        #: `_background` and `_closing` are mutated from the SDK's threads
        #: (`_fire` adding) and from the scheduler loop (the done-callback
        #: discarding, and `_drain_recordings` snapshotting and closing).
        #: One lock for both because it is the *pair* that has to move
        #: together: `_drain_recordings` may only set `_closing` in the same
        #: critical section that found `_background` empty, or a recording
        #: fired in between is neither waited for nor refused.
        self._background_lock = threading.Lock()
        #: Set by `_drain_recordings` once nothing is left in flight, so a
        #: recording fired after that point is refused loudly instead of
        #: becoming a task the loop teardown destroys silently. Cleared at
        #: the top of every `run()`, because the same instance is run again
        #: by `_supervised_connection` after a failure.
        self._closing = False
        #: Set once `connect_until_ready` has returned, so the recording
        #: paths can tell "a live socket dropped" from "this never came up".
        #: Not cleared on the SDK's own reconnects — it reconnects underneath
        #: us and the socket stays this instance's — but cleared at the top
        #: of every `run()`: `_supervised_connection` calls `run()` again on
        #: *this same object* after a failure, so a flag left set by the
        #: previous round would make the next round's failed connect look
        #: like a live socket dropping.
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
        `deliver` 炸了之后在 `except` 里现取——`event_id_of` 自己保证不会
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

        event_id = event_id_of(envelope)
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

    async def record_connect_failed(self) -> bool:
        """The one trace a connection that never came up leaves behind.

        **Returns whether the row actually landed**, which is not the same
        question as whether this was called. Every failure here is
        swallowed — an audit table that is briefly unreachable must not take
        the socket down with it — so a caller that treats "I called it" as
        "the row exists" will later close an outage that has no left edge,
        and §19.2 would read a `reconnected` opening onto nothing. The
        boolean is what lets `_supervised_connection` tell the two apart.

        Without it the most common production outage — the app is not
        enabled for long connection, the secret is wrong, Feishu is
        unreachable at startup — is invisible to §19.2's redelivery check
        and to the console, and shows up only as a log line inside the
        scheduler container.

        **Public, and `run()` does not call it.** `run()` is one attempt;
        the run of attempts belongs to whoever retries (in this deployment,
        `_supervised_connection`), and only that caller can tell the first
        failure of an outage from the two-hundredth. Writing one of these
        per attempt is what buried every other audit row for the workspace
        behind ~262 rows a day of a socket that was never coming up.
        """
        if self._record is None:
            return False
        try:
            await self._record(self._binding.binding_id, "connect_failed", None)
        except Exception:
            logger.exception(
                "failed to record connect failure binding=%s", self._binding.binding_id
            )
            return False
        return True

    async def record_recovered(self, down_seconds: float) -> None:
        """Closes an outage whose `connect_failed` row is really in the
        table, with its length.

        The pair to `record_connect_failed`, public for the same reason: the
        retry loop owns the outage, so it is the only place that knows when
        a run of failures ended. Not optional either — §19.2 reads the two
        rows as a window, and a window with only a left edge measures
        nothing, so a capped `connect_failed` without this would trade a
        flooded audit page for an unmeasurable one.

        Which left edge exists is the caller's to know, and this method
        cannot check it: `_supervised_connection` calls this only when
        `record_connect_failed` returned `True`. A window with only a
        *right* edge is the failure mode that guard exists for.

        `max(0.0, ...)` for the same reason as `_record_reconnected`: a
        negative duration would be a worse answer than a slightly-off
        positive one.
        """
        if self._record is None:
            return
        try:
            await self._record(
                self._binding.binding_id, "reconnected", max(0.0, down_seconds)
            )
        except Exception:
            logger.exception(
                "failed to record recovery binding=%s", self._binding.binding_id
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
        with self._background_lock:
            if self._closing:
                # `_drain_recordings` has already found nothing left in
                # flight and will not look again, so this recording would
                # become a task nobody awaits: the loop teardown destroys it
                # pending, the transaction rolls back, and asyncio's "Task
                # was destroyed but it is pending" names neither the binding
                # nor the row. Refusing it loses the same row and says so.
                coro.close()
                logger.error(
                    "long connection dropped a %s recording that arrived after"
                    " the connection closed binding=%s",
                    getattr(coro, "__qualname__", "connection"),
                    self._binding.binding_id,
                )
                return
            # Submitting inside the lock is what makes the check above
            # binding: a future created here is in `_background` before the
            # lock is released, so `_drain_recordings` either waits for it or
            # (having closed) never let it be created. `add_done_callback`
            # stays outside — on an already-completed future it runs the
            # callback in *this* thread, and `_forget` takes this same
            # non-reentrant lock.
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            self._background.add(future)
        future.add_done_callback(self._forget)

    def _forget(self, future: "Future[None]") -> None:
        with self._background_lock:
            self._background.discard(future)

    async def _drain_recordings(self) -> None:
        """Waits for every recording still in flight, then shuts the door.

        Looping rather than taking one snapshot: `_fire` is called from the
        SDK's threads, and `disconnect()` does not join them —
        `_stop_private_ws_client` only asks the module-level ws loop to stop
        — so a late `_on_reconnecting` can queue a recording *while this is
        already waiting on the gather*. A single snapshot would return
        without it, `run()` would return, and `asyncio.run` would destroy
        that write mid-transaction.

        The empty check and `_closing` are set in one critical section, so
        there is no instant at which a recording is neither waited for nor
        refused.

        **`disconnect()` having run is not what makes this terminate.** That
        it does not stop the callbacks is the whole premise above — it only
        *asks* the module-level ws loop to stop. What actually ends the
        supply of new recordings is that loop finally stopping, which is on
        the SDK's clock, not this one's; a socket that keeps signalling
        while it is torn down can keep this waiting. So the loop is bounded
        by `DRAIN_TIMEOUT_SECONDS`. When it runs out the recordings still in
        flight are *cancelled* — `asyncio.wait_for` cancels the gather, which
        cancels each wrapper, which cancels the task
        `run_coroutine_threadsafe` chained to it — and the count is logged.
        That is a real loss, the same rows a reader will later not find, but
        cancelling and saying so beats both an unbounded wait that parks the
        scheduler's shutdown with no line saying where, and a silent return
        that leaves the loop teardown to destroy the tasks.

        The count is read *before* the wait, not after: cancellation runs
        `_forget` for each of them, so a count taken afterwards races with
        the callbacks and would report zero abandoned recordings on a
        shutdown that abandoned several.
        """
        deadline = time.monotonic() + DRAIN_TIMEOUT_SECONDS
        while True:
            with self._background_lock:
                pending = tuple(self._background)
                if not pending:
                    self._closing = True
                    return
            try:
                # Every way out of this loop other than "nothing left" goes
                # through here, which is what makes the cancellation the log
                # line below claims universal: an already-spent deadline is
                # `timeout=0.0`, and `wait_for` cancels before raising
                # rather than returning the futures untouched.
                await asyncio.wait_for(
                    asyncio.gather(
                        *(asyncio.wrap_future(future) for future in pending),
                        return_exceptions=True,
                    ),
                    timeout=max(0.0, deadline - time.monotonic()),
                )
            except TimeoutError:
                break
        with self._background_lock:
            # Having stopped waiting, this must also stop accepting, or
            # `_fire` would keep handing new work to a loop that is about to
            # be torn down.
            self._closing = True
        logger.error(
            "long connection gave up waiting for %d recording(s) after %.0fs and"
            " cancelled them binding=%s; whatever they were writing is lost",
            len(pending),
            DRAIN_TIMEOUT_SECONDS,
            self._binding.binding_id,
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
                " came up; not recorded as a disconnect (whether a failed"
                " connect is worth a row is the retry loop's call)",
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

    def _reset_for_this_run(self) -> None:
        """Clears everything the *previous* `run()` left behind.

        `_supervised_connection` retries by calling `run()` again on this
        same object, so none of this is per-instance state — it is per-call
        state that happens to live on the instance because the SDK's
        callbacks can only reach the instance. A `_connected` carried over
        from a round that came up and then failed made the next round's
        failed connect look like a live socket dropping: `_on_reconnecting`
        would write a `"disconnected"` row for a socket that never came up
        that round, and `_down_since` carried over with it would fold the
        whole retry backoff into the next `down_seconds`.
        """
        self._connected = False
        self._down_since = None
        with self._background_lock:
            self._closing = False

    async def run(
        self,
        stop: asyncio.Event,
        *,
        on_connected: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Own one `FeishuChannel` for the lifetime of this call.

        `on_connected` is awaited the instant the socket is up and before
        this parks on `stop`, which is the only moment a caller can learn
        that a run of failed attempts has ended — `run()` itself does not
        return until shutdown. Awaited, not fired and forgotten, so a caller
        that records something there has actually recorded it before the
        socket starts delivering; anything it raises comes out of `run()`
        like any other post-connect failure.

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
        one who decides whether that means retry or give up, **and whether
        this failure is worth an audit row** — see `record_connect_failed`.
        This method neither swallows the exception nor audits it.
        """
        self._reset_for_this_run()
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
            if on_connected is not None:
                await on_connected()
            await stop.wait()
        finally:
            # Nested, not two statements in a row: `disconnect()` raises in
            # production (`FeishuChannel.stop` reaches into the SDK's private
            # ws client and a teardown that goes wrong comes back out here),
            # and as a bare first statement it took the drain with it — every
            # recording still in flight abandoned mid-write, `_closing` left
            # `False`, and the loop teardown destroying the task with only
            # asyncio's "Task was destroyed but it is pending" to show for
            # it. The drain is the part that has to happen on every path;
            # the teardown error is still the exception that propagates.
            try:
                await channel.disconnect()
            finally:
                await self._drain_recordings()
