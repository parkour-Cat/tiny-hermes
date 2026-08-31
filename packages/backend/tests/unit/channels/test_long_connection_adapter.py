"""适配器只做一件事：把 SDK 交给它的帧，原样交给共用的那一半。

它不认识 `FeishuWebhookService`，只认识 `DeliverFrame` 协议——所以这一层的
测试不需要数据库，也不该需要。
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
from tiny_hermes.channels.application.webhook_service import Claimed, Unreadable
from tiny_hermes.channels.infrastructure.feishu_long_connection import (
    FeishuLongConnection,
    LongConnectionBinding,
)

#: `DeliverFrame` returns whatever `accept_verified` returns. `on_frame`
#: never reads it — see its docstring — but the fixtures below still have to
#: hand back *something* of that type to satisfy the protocol, so this is a
#: throwaway value, not a claim about what a real delivery produced.
_DUMMY_RESULT = Unreadable(kind="unused", external_user_id="ou_unused", claim_id=None)


@dataclass
class _Frame:
    """一个假的 SDK 帧对象——只带 `.raw`，因为那是 `_envelope_of` 唯一读的属性。

    真实的 `InboundMessage` 还有 `chat_id`、`content_text` 等字段，这里不需要：
    `on_frame` 从不读它们，加上反而会让这个测试看起来在断言 SDK 的整个形状。
    """

    raw: dict[str, Any]


def _frame(payload: dict[str, Any]) -> _Frame:
    return _Frame(raw=payload)


class _DeliverSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, dict[str, Any]]] = []

    async def __call__(
        self, binding_id: UUID, envelope: dict[str, Any]
    ) -> Claimed | Unreadable:
        self.calls.append((binding_id, envelope))
        return _DUMMY_RESULT


class _DeliverBoom:
    """一个会抛的 `deliver`——具体是哪种异常不重要，`on_frame` 的 `except Exception`
    不区分来源。`RuntimeError` 只是任选一个非 `BaseException` 的具体类型，不是在
    还原 `accept_verified` 真实会抛的那一种。"""

    async def __call__(
        self, binding_id: UUID, envelope: dict[str, Any]
    ) -> Claimed | Unreadable:
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
