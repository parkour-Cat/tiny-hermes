"""适配器只做一件事：把 SDK 交给它的帧，原样交给共用的那一半。

它不认识 `FeishuWebhookService`，只认识 `DeliverFrame` 协议——所以这一层的
测试不需要数据库，也不该需要。
"""

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
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
    binding = LongConnectionBinding(binding_id=uuid4(), app_id="cli_x", app_secret="s")
    adapter = FeishuLongConnection(binding, deliver_spy)

    await adapter.on_frame(_frame({"schema": "2.0", "header": {"event_id": "e1"}}))

    assert deliver_spy.calls == [
        (binding.binding_id, {"schema": "2.0", "header": {"event_id": "e1"}})
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
