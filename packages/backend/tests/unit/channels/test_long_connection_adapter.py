"""适配器只做一件事：把 SDK 交给它的帧，原样交给共用的那一半。

它不认识 `FeishuWebhookService`，只认识 `DeliverFrame` 协议——所以这一层的
测试不需要数据库，也不该需要。
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
from lark_oapi.channel.safety import (  # pyright: ignore[reportMissingTypeStubs]
    SafetyPipeline,
)
from lark_oapi.channel.types import (  # pyright: ignore[reportMissingTypeStubs]
    Conversation,
    FileContent,
    Identity,
    InboundMessage,
    TextContent,
)
from tiny_hermes.channels.infrastructure.feishu_long_connection import (
    FeishuLongConnection,
    LongConnectionBinding,
)


@dataclass
class _Frame:
    """一个假的 SDK 帧对象——只带 `.raw`，因为那是 `_envelope_of` 唯一读的属性。

    真实的 `InboundMessage` 还有 `chat_id`、`content_text` 等字段，这里不需要：
    `on_frame` 从不读它们，加上反而会让这个测试看起来在断言 SDK 的整个形状。

    `raw` 的类型是 `Any` 而不是 `dict[str, Any]`——`on_frame` 拿到的
    `frame.raw` 也没有静态类型保证，SDK 交上来的东西不受这个模块控制，
    下面 `test_a_non_dict_raw_still_leaves_one_log_line` 就是在验证「不是
    `dict`」这个真实可能发生的情况。
    """

    raw: Any


def _frame(payload: dict[str, Any]) -> _Frame:
    return _Frame(raw=payload)


class _DeliverSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, dict[str, Any]]] = []

    async def __call__(self, binding_id: UUID, envelope: dict[str, Any]) -> None:
        self.calls.append((binding_id, envelope))


class _DeliverBoom:
    """一个会抛的 `deliver`——具体是哪种异常不重要，`on_frame` 的 `except Exception`
    不区分来源。`RuntimeError` 只是任选一个非 `BaseException` 的具体类型，不是在
    还原 `accept_verified` 真实会抛的那一种。"""

    async def __call__(self, binding_id: UUID, envelope: dict[str, Any]) -> None:
        del binding_id, envelope
        raise RuntimeError("frame not handled")


@pytest.fixture
def deliver_spy() -> _DeliverSpy:
    return _DeliverSpy()


@pytest.fixture
def deliver_boom() -> _DeliverBoom:
    return _DeliverBoom()


async def test_a_frame_is_handed_to_the_shared_half(deliver_spy: _DeliverSpy) -> None:
    """递过去的必须是共用那一半读得懂的信封，不是 SDK 原样的帧。

    这条测试原来把一个手搭的 webhook 信封塞进 `frame.raw` 再断言它原样传出去
    ——两边都是同一个虚构，所以它绿得毫无意义。真机第一条消息证明 SDK 给的是
    **消息对象**，事件 id 根本不在里面。现在喂 SDK 自己的 `InboundMessage`，
    断言的是 `_envelope_of` 重建出来的形状。
    """
    binding = LongConnectionBinding(binding_id=uuid4(), app_id="cli_x", app_secret="s")
    adapter = FeishuLongConnection(binding, deliver_spy)

    await adapter.on_frame(
        InboundMessage(
            id="om_1",
            create_time=0,
            conversation=Conversation(chat_id="oc_1", chat_type="p2p"),
            sender=Identity(open_id="ou_zhang"),
            raw={"message_id": "om_1", "message_type": "text", "content": '{"text": "hi"}'},
        )
    )

    assert deliver_spy.calls == [
        (
            binding.binding_id,
            {
                "header": {"event_id": "om_1"},
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_zhang"}},
                    "message": {
                        "message_id": "om_1",
                        "message_type": "text",
                        "content": '{"text": "hi"}',
                    },
                },
            },
        )
    ]


async def test_a_failing_frame_does_not_kill_the_connection(
    deliver_boom: _DeliverBoom,
) -> None:
    # 一条读不懂或处理失败的消息，不能让整条连接断掉——断了之后所有后续
    # 消息都收不到，代价远大于丢这一条。
    binding = LongConnectionBinding(binding_id=uuid4(), app_id="cli_x", app_secret="s")
    adapter = FeishuLongConnection(binding, deliver_boom)

    await adapter.on_frame(_frame({"header": {"event_id": "e2"}}))

    assert adapter.alive is True


async def test_a_non_dict_raw_still_leaves_one_log_line(
    deliver_boom: _DeliverBoom, caplog: pytest.LogCaptureFixture
) -> None:
    """`frame.raw` not being a `dict` is exactly the "frame this build cannot
    read" case `on_frame` exists to survive. A version that only `cast()`s
    it without checking lets a non-dict through as if it were the envelope;
    `deliver_boom` then fails on it, and the failure path itself reads the
    envelope again to find an event id to log — on a non-dict, that second
    read is what used to raise and skip the log line entirely, escaping
    `on_frame` as an unhandled `AttributeError` instead of being caught by
    its own `except Exception`. Both promises the docstring makes — nothing
    escapes, and every failure gets exactly one log line — are pinned here
    together, because the earlier bug broke both at once.
    """
    binding = LongConnectionBinding(binding_id=uuid4(), app_id="cli_x", app_secret="s")
    adapter = FeishuLongConnection(binding, deliver_boom)

    with caplog.at_level(logging.ERROR):
        await adapter.on_frame(_Frame(raw="not a dict"))

    assert adapter.alive is True
    assert len(caplog.records) == 1


def _message(message_id: str, kind: str, content: str, text: str) -> InboundMessage:
    return InboundMessage(
        id=message_id,
        # Now, not zero: the safety pipeline drops anything older than its
        # stale window before it gets as far as batching.
        create_time=int(time.time() * 1000),
        conversation=Conversation(chat_id="oc_1", chat_type="p2p"),
        sender=Identity(open_id="ou_zhang"),
        content=FileContent(file_key="file_v3_1", file_name="orders.xlsx")
        if kind == "file"
        else TextContent(text=text),
        raw_content_type=kind,
        raw={"message_id": message_id, "message_type": kind, "content": content},
    )


async def test_a_file_sent_with_its_question_reaches_the_shared_half(
    deliver_spy: _DeliverSpy,
) -> None:
    """Feishu sends a file and the words typed beside it as two messages in
    the same instant. With the SDK's default text batching those two were
    merged into one whose `raw` is the last message's — the words — and the
    file was gone before `on_frame` ever ran: no row, no log line. Found by
    a real file in a real chat, not by a test.

    The pipeline here is the SDK's own, built from the safety config the
    adapter's channel actually carries, with the arguments `FeishuChannel`
    builds it with — so it is the SDK's merge that is under test, not a copy
    of what we believe it does.
    """
    binding = LongConnectionBinding(binding_id=uuid4(), app_id="cli_x", app_secret="s")
    adapter = FeishuLongConnection(binding, deliver_spy)
    safety = adapter.channel().config.safety
    pipeline = SafetyPipeline(
        loop=asyncio.get_running_loop(),
        on_message=adapter.on_frame,
        dedup_config=safety.dedup,
        batch_config=safety.text_batch,
        media_batch_config=safety.media_batch,
        queue_config=safety.chat_queue,
        stale_window_ms=safety.stale_message_window_ms,
    )

    await pipeline.push_message(
        _message("om_file", "file", '{"file_key": "file_v3_1", "file_name": "orders.xlsx"}', "")
    )
    await pipeline.push_message(_message("om_text", "text", '{"text": "汇总一下"}', "汇总一下"))
    await pipeline.dispose()

    delivered = {envelope["header"]["event_id"]: envelope for _, envelope in deliver_spy.calls}
    assert set(delivered) == {"om_file", "om_text"}
    assert delivered["om_file"]["event"]["message"]["message_type"] == "file"
