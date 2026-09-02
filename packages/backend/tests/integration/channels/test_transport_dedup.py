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
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
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


@dataclass
class _Frame:
    """`on_frame`只读`.raw` —— 见`feishu_long_connection._envelope_of`。"""

    raw: dict[str, Any]


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


async def test_the_same_event_over_both_transports_makes_one_run(
    service: FeishuWebhookService,
    store: SqlChannelStore,
    seeded_binding: tuple[UUID, str],
) -> None:
    binding_id, _workspace_id = seeded_binding
    envelope = _text_message_envelope("hello", event_id="dup-1")

    # 第一条：webhook 路径已经在用的那半——直接调用 `accept_verified`，就像
    # `accept()` 验完签、解完密之后做的那样。
    first = await service.accept_verified(binding_id=binding_id, envelope=envelope)

    # 第二条：真的经过适配器。`deliver` 就是这同一个 `service`，`results`
    # 记的是这次调用里 `accept_verified` 交出来的东西——适配器自己不读返回值
    # （`DeliverFrame` 返回 `None`），所以这个列表是唯一能看见它确实调用过、
    # 以及调用之后发生了什么的地方。
    results: list[Claimed | Unreadable] = []

    async def _deliver(binding_id: UUID, envelope: dict[str, Any]) -> None:
        results.append(await service.accept_verified(binding_id=binding_id, envelope=envelope))

    binding = LongConnectionBinding(binding_id=binding_id, app_id="cli_x", app_secret="s")
    adapter = FeishuLongConnection(binding, _deliver)
    await adapter.on_frame(_Frame(raw=envelope))

    assert isinstance(first, Claimed)
    assert first.claim_id is not None

    # `on_frame` 必须真的抵达了共用的 `deliver`——如果它被自己的一份去重
    # 拦在半路，`results` 会是空的，下面这行先红，而不是让人以为
    # `_events_for` 那行的绿色就足够了。
    assert len(results) == 1
    second = results[0]
    assert isinstance(second, Claimed)
    assert second.claim_id is None

    assert await _events_for(store, binding_id, "dup-1") == 1
