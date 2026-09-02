"""同一条事件经两条路到达，只产生一个 Run。

两种 transport 共用同一个认领，所以第二条会被 `(binding_id, channel_event_id)`
挡掉。若将来有人给长连接另写一份去重，这条测试会红——那正是它存在的理由。

这条测试真的走两条 transport，不是调用两次 `accept_verified` 假装走了两条路：
第一条通过 webhook 路径已经用的 `accept_verified`，第二条通过
`FeishuLongConnection.on_frame`，`deliver` 接的是同一个真实 `service`。只断言
`channel_events` 的行数不够——那个数字即使 `on_frame` 完全没调用 `deliver`
（比如被另一份自己的去重直接吞掉）也照样是 1，因为第一条 webhook 投递已经写了
那一行。所以还要断言 `on_frame` 那次投递本身确实抵达了共用的 `deliver`，并且
它看到的是"这是重复"——见下面 `_events_for` 之前的两条断言。
"""

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from lark_oapi.channel.types import (  # pyright: ignore[reportMissingTypeStubs]
    Conversation,
    Identity,
    InboundMessage,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.channels.application.webhook_service import (
    Claimed,
    FeishuWebhookService,
    Unreadable,
)
from tiny_hermes.channels.infrastructure.feishu_long_connection import (
    FeishuLongConnection,
    LongConnectionBinding,
)
from tiny_hermes.channels.infrastructure.sql_channel_store import SqlChannelStore


def _text_message_envelope(text_body: str, *, event_id: str) -> dict[str, Any]:
    return {
        "header": {"event_id": event_id},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_zhang"}},
            "message": {"content": json.dumps({"text": text_body})},
        },
    }


async def _events_for(store: SqlChannelStore, binding_id: UUID, channel_event_id: str) -> int:
    # A fresh connection would open its own transaction and, under READ
    # COMMITTED, might not see what `service` wrote through `store`'s session
    # if that session has not committed yet. Counting through the same
    # session is what makes this see exactly what the two deliveries above
    # just did.
    found = await store._session.execute(  # noqa: SLF001 # pyright: ignore[reportPrivateUsage]
        text(
            "SELECT count(*) FROM channel_events"
            " WHERE channel_binding_id = :b AND channel_event_id = :e"
        ),
        {"b": binding_id, "e": channel_event_id},
    )
    return int(found.scalar_one())


@pytest.fixture
async def seeded_binding(
    engine: AsyncEngine, workspace_id: str, published_agent: str
) -> tuple[UUID, str]:
    binding_id = uuid4()
    async with engine.begin() as connection:
        owner = await connection.execute(text("SELECT id FROM users LIMIT 1"))
        await connection.execute(
            text(
                "INSERT INTO channel_bindings"
                " (id, workspace_id, channel, agent_id, status, created_by,"
                "  created_at, encrypt_key_ref)"
                " VALUES (:i, :w, 'feishu', :a, 'active', :u, now(), :k)"
            ),
            {
                "i": binding_id,
                "w": UUID(workspace_id),
                "a": UUID(published_agent),
                "u": owner.scalar_one(),
                "k": "TEST_KEY",
            },
        )
    return binding_id, workspace_id


@pytest.fixture
async def store(engine: AsyncEngine) -> AsyncIterator[SqlChannelStore]:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        yield SqlChannelStore(session)


@pytest.fixture
def service(store: SqlChannelStore) -> FeishuWebhookService:
    return FeishuWebhookService(store)


def _inbound(body: str, *, message_id: str) -> InboundMessage:
    """What the SDK actually hands a `MESSAGE` handler — its own dataclass,
    not a dict shaped the way this repository wished the SDK worked."""
    return InboundMessage(
        id=message_id,
        create_time=0,
        conversation=Conversation(chat_id="oc_test", chat_type="p2p"),
        sender=Identity(open_id="ou_zhang"),
        raw={
            "message_id": message_id,
            "chat_id": "oc_test",
            "message_type": "text",
            "content": json.dumps({"text": body}),
        },
        content_text=body,
        raw_content_type="text",
    )


async def test_the_long_connection_carries_no_dedup_of_its_own(
    service: FeishuWebhookService,
    store: SqlChannelStore,
    seeded_binding: tuple[UUID, str],
) -> None:
    """同一帧来两次，只产生一个认领——靠的是共用的那一份，不是适配器自己的。

    这条测试原来断言的是「同一事件先后经两条 transport 只产生一个 Run」。
    那个断言建立在一个**错误的前提**上：以为两条 transport 拿到同一个信封。
    真机走查证明不是——SDK 的 `InboundMessage` 根本不带飞书的事件 id
    （见 `_envelope_of` 的 docstring），所以长连接的认领键是 `message_id`，
    与 webhook 的 `event_id` 不在同一个键空间。设计文档 §3 已按此修订，
    理由写在那里。

    修订之后**仍然成立、也仍然要守**的是这一条：适配器不许自带去重。
    如果它自己拦掉了第二帧，`results` 会只有一条，下面那行先红——而不是让
    「认领数是 1」这个绿色替一个错误的原因背书。
    """
    binding_id, _workspace_id = seeded_binding

    results: list[Claimed | Unreadable] = []

    async def _deliver(binding_id: UUID, envelope: dict[str, Any]) -> None:
        results.append(await service.accept_verified(binding_id=binding_id, envelope=envelope))

    binding = LongConnectionBinding(binding_id=binding_id, app_id="cli_x", app_secret="s")
    adapter = FeishuLongConnection(binding, _deliver)

    frame = _inbound("hello", message_id="om_dup_1")
    await adapter.on_frame(frame)
    await adapter.on_frame(frame)

    # 两帧都抵达了共用的 `deliver`：适配器没有在半路拦第二帧。
    assert len(results) == 2
    first, second = results
    assert isinstance(first, Claimed)
    assert first.claim_id is not None
    assert isinstance(second, Claimed)
    assert second.claim_id is None, "第二帧应当认领落空，这正是共用去重在起作用"

    assert await _events_for(store, binding_id, "om_dup_1") == 1


async def test_the_two_transports_no_longer_share_a_dedup_key(
    service: FeishuWebhookService,
    store: SqlChannelStore,
    seeded_binding: tuple[UUID, str],
) -> None:
    """这条钉的是**代价**，不是成绩。

    设计文档原来要求两条 transport 共用一个键空间。SDK 不给事件 id，做不到，
    于是产品决定接受：飞书的投递方式是二选一的（开发者后台单选），两路同时
    收到同一条消息只可能发生在切换的那一瞬间。

    把它写成测试而不是只写进文档，是因为这是一条**会被悄悄改回去**的语义：
    将来谁若让两边的键重新对上，这条测试会红，那时该做的是回来改文档，
    而不是删掉这条断言。
    """
    binding_id, _workspace_id = seeded_binding

    webhook = await service.accept_verified(
        binding_id=binding_id, envelope=_text_message_envelope("hello", event_id="dup-1")
    )

    results: list[Claimed | Unreadable] = []

    async def _deliver(binding_id: UUID, envelope: dict[str, Any]) -> None:
        results.append(await service.accept_verified(binding_id=binding_id, envelope=envelope))

    adapter = FeishuLongConnection(
        LongConnectionBinding(binding_id=binding_id, app_id="cli_x", app_secret="s"),
        _deliver,
    )
    await adapter.on_frame(_inbound("hello", message_id="om_dup_2"))

    assert isinstance(webhook, Claimed)
    assert webhook.claim_id is not None
    over_the_socket = results[0]
    assert isinstance(over_the_socket, Claimed)
    # 两个键空间，所以两个认领。这是修订后的语义，不是缺陷。
    assert over_the_socket.claim_id is not None
    assert over_the_socket.claim_id != webhook.claim_id
    assert await _events_for(store, binding_id, "dup-1") == 1
    assert await _events_for(store, binding_id, "om_dup_2") == 1
