"""同一条事件经两条路到达，只产生一个 Run。

两种 transport 共用同一个认领，所以第二条会被 `(binding_id, channel_event_id)`
挡掉。若将来有人给长连接另写一份去重，这条测试会红——那正是它存在的理由。
"""

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.channels.application.webhook_service import Claimed, FeishuWebhookService
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
    # session is what makes this see exactly what the two `accept_verified`
    # calls above just did.
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

    first = await service.accept_verified(binding_id=binding_id, envelope=envelope)
    second = await service.accept_verified(binding_id=binding_id, envelope=envelope)

    assert isinstance(first, Claimed)
    assert isinstance(second, Claimed)
    assert first.claim_id is not None
    assert second.claim_id is None
    assert await _events_for(store, binding_id, "dup-1") == 1
