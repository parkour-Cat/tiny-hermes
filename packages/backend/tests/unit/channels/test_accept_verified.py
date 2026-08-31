"""已经验过签、解过密的事件，从这里进来。

长连接的帧由飞书 SDK 验签与解密，所以它不能走 `accept()` 的前半段；两种
transport 共用的是后半段——归一化与认领。去重就在认领里，只有这一份。
"""

import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from tiny_hermes.channels.application.webhook_service import (
    Claimed,
    FeishuWebhookService,
    Unreadable,
)


class SpyClaims:
    def __init__(self) -> None:
        self.claimed: list[tuple[UUID, str]] = []

    async def claim_delivery(
        self, binding_id: UUID, channel_event_id: str, now: datetime
    ) -> UUID | None:
        del now
        self.claimed.append((binding_id, channel_event_id))
        return uuid4()


@pytest.fixture
def claims_spy() -> SpyClaims:
    return SpyClaims()


def _text_message_envelope(text: str) -> dict[str, Any]:
    return {
        "header": {"event_id": "om_1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_zhang"}},
            "message": {"content": json.dumps({"text": text})},
        },
    }


def _unsupported_envelope() -> dict[str, Any]:
    return {
        "header": {"event_id": "om_2"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_zhang"}},
            "message": {"message_type": "audio", "content": json.dumps({"file_key": "f_1"})},
        },
    }


async def test_a_verified_event_is_normalized_and_claimed(claims_spy: SpyClaims) -> None:
    service = FeishuWebhookService(claims_spy)
    binding = uuid4()

    answer = await service.accept_verified(
        binding_id=binding, envelope=_text_message_envelope("hello")
    )

    assert isinstance(answer, Claimed)
    assert answer.event.text == "hello"
    assert claims_spy.claimed == [(binding, answer.event.channel_event_id)]


async def test_an_unreadable_event_still_gets_claimed(claims_spy: SpyClaims) -> None:
    # 认领是去重，不是「这条能不能处理」。一条读不懂的消息也必须占住它的
    # `channel_event_id`，否则飞书重投时会被当成新消息再走一遍。
    service = FeishuWebhookService(claims_spy)

    answer = await service.accept_verified(
        binding_id=uuid4(), envelope=_unsupported_envelope()
    )

    assert isinstance(answer, Unreadable)
    assert len(claims_spy.claimed) == 1


async def test_it_does_not_decrypt(claims_spy: SpyClaims) -> None:
    # 传进来的信封已经是明文。这条测试钉住签名里没有 `encrypt_key`：
    # 若将来有人把解密挪回这一层，它会因为拿不到密钥而失败。
    import inspect

    signature = inspect.signature(FeishuWebhookService.accept_verified)
    assert "encrypt_key" not in signature.parameters
    assert "secrets" not in signature.parameters
