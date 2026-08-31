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
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import UUID

from lark_oapi.channel import (  # pyright: ignore[reportMissingTypeStubs]
    Events,
    FeishuChannel,
)

from tiny_hermes.channels.application.webhook_service import Claimed, Unreadable
from tiny_hermes.channels.domain._json import object_at, string_at

logger = logging.getLogger(__name__)


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


def _envelope_of(frame: Any) -> dict[str, Any]:
    """The SDK's `InboundMessage.raw` is Feishu's own event JSON — the same
    shape `event_from_envelope` already reads off the decrypted webhook body
    (see `doc/channel/reference.md`'s Message Model: "`raw` — Original event
    payload"). Both transports converge on one reader because there is one
    envelope shape to read, not two.
    """
    return cast(dict[str, Any], frame.raw)


def _event_id_of(envelope: dict[str, Any]) -> str:
    """Best-effort id for the one log line `on_frame` writes on failure.

    Mirrors `event_from_envelope`'s two schema versions (`header.event_id`
    for v2, top-level `uuid` for v1) using the same `object_at`/`string_at`
    readers that function uses, rather than a second hand-rolled pair of
    `isinstance` checks — this is not a claim that the envelope is
    well-formed, only an attempt to name it so a failure can be found later.
    A malformed envelope that carries neither is exactly the case worth
    being able to search for, so it gets a literal placeholder rather than a
    log line built with no event id in it at all.
    """
    header = object_at(envelope, "header")
    event_id = string_at(header, "event_id") if header else None
    if event_id is not None:
        return event_id
    top_level = string_at(envelope, "uuid")
    if top_level is not None:
        return top_level
    return "<no event id>"


class FeishuLongConnection:
    def __init__(self, binding: LongConnectionBinding, deliver: DeliverFrame) -> None:
        self._binding = binding
        self._deliver = deliver
        #: `on_frame` never clears this on a delivery failure — that is the
        #: one guarantee this attribute carries right now. Nothing else in
        #: this adapter writes to it either, so it does not yet reflect
        #: whether the underlying socket is actually still open; hooking it
        #: up to the SDK's own `reconnecting`/`error` events is for whoever
        #: hosts this in the scheduler process to decide, not this adapter.
        self.alive = True

    async def on_frame(self, frame: Any) -> None:
        """把一帧交给两种 transport 共用的那一半。

        异常在这里被吃掉而不是冒泡：一条读不懂或处理失败的消息若把连接带下去，
        之后所有消息都收不到，代价远大于丢这一条。日志是这条路径上唯一的痕迹，
        所以它必须带上 `binding_id` 和事件 id——事件 id 在 `try` 之外先取好，
        这样即使 `deliver` 本身炸了，日志里仍然有它，而不是只有
        `binding_id` 却认不出是哪条消息。

        `_envelope_of(frame)` 本身也可能失败（`frame.raw` 缺失或不是
        `dict`）；这种连事件 id 都读不出来的帧不该在日志里静悄悄地什么都不
        写，所以那种情况写一个占位符而不是省略这一行。
        """
        try:
            envelope = _envelope_of(frame)
        except Exception:
            logger.exception(
                "long connection frame not handled binding=%s event=<unreadable frame>",
                self._binding.binding_id,
            )
            return

        try:
            await self._deliver(self._binding.binding_id, envelope)
        except Exception:
            logger.exception(
                "long connection frame not handled binding=%s event=%s",
                self._binding.binding_id,
                _event_id_of(envelope),
            )

    async def run(self, stop: asyncio.Event) -> None:
        """Own one `FeishuChannel` for the lifetime of this call.

        `connect_until_ready()` rather than the blocking `connect()`: the
        quickstart recommends it for exactly this shape of caller — one that
        needs control back so it can wait on its own stop signal instead of
        handing the event loop to the SDK. `disconnect()` in `finally` so a
        cancelled `stop.wait()` still closes the socket rather than leaking
        it.

        `timeout=30` is spelled out rather than left to the SDK's own
        default (which happens to also be 30) because what happens when it
        expires is worth being explicit about: `_wait_background_start_ready`
        (`lark_oapi/channel/channel.py`) stops the transport and raises
        `FeishuChannelError(NOT_CONNECTED, "Timed out waiting for channel
        transport readiness")`, which propagates out of this `run()` call
        uncaught. Whoever hosts this adapter in the scheduler process is the
        one who decides whether that means retry or give up — this method
        does not swallow it.
        """
        channel = FeishuChannel(
            app_id=self._binding.app_id, app_secret=self._binding.app_secret
        )
        channel.on(Events.MESSAGE, self.on_frame)
        await channel.connect_until_ready(timeout=30)
        try:
            await stop.wait()
        finally:
            await channel.disconnect()
