"""绑定说明自己用哪种方式收事件。

默认 `webhook`：既有绑定靠公网地址收消息，默认值若是长连接，它们会在升级的
那一刻集体失聪。
"""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.channels.infrastructure.sql_binding_store import SqlChannelBindingStore


@pytest.fixture
async def store(engine: AsyncEngine) -> AsyncIterator[SqlChannelBindingStore]:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        yield SqlChannelBindingStore(db)


@pytest.fixture
async def seeded_binding(
    engine: AsyncEngine, workspace_id: str, published_agent: str
) -> tuple[UUID, UUID]:
    """Inserted with raw SQL, `transport` column untouched — the point is to
    read back what an existing row (created before this column existed)
    gets from the column's own default, not from anything the ORM layer
    supplies at insert time.
    """
    binding_id = uuid4()
    async with engine.begin() as connection:
        user = await connection.execute(text("SELECT id FROM users LIMIT 1"))
        await connection.execute(
            text(
                "INSERT INTO channel_bindings"
                " (id, workspace_id, channel, agent_id, status, created_by,"
                "  created_at, encrypt_key_ref)"
                " VALUES (:i, :w, 'feishu', :a, 'active', :u, now(), 'TEST_KEY')"
            ),
            {
                "i": binding_id,
                "w": UUID(workspace_id),
                "a": UUID(published_agent),
                "u": user.scalar_one(),
            },
        )
    return binding_id, UUID(workspace_id)


async def test_an_existing_binding_reads_back_as_webhook(
    store: SqlChannelBindingStore, seeded_binding: tuple[UUID, UUID]
) -> None:
    binding_id, workspace_id = seeded_binding

    binding = await store.binding(workspace_id, binding_id)

    assert binding is not None
    assert binding.transport == "webhook"


async def test_a_binding_can_declare_long_connection(
    store: SqlChannelBindingStore, seeded_binding: tuple[UUID, UUID]
) -> None:
    binding_id, workspace_id = seeded_binding

    await store.set_transport(workspace_id, binding_id, "long_connection")

    binding = await store.binding(workspace_id, binding_id)
    assert binding is not None
    assert binding.transport == "long_connection"


async def test_an_invented_transport_is_refused(
    store: SqlChannelBindingStore, seeded_binding: tuple[UUID, UUID]
) -> None:
    binding_id, workspace_id = seeded_binding

    with pytest.raises(Exception):
        await store.set_transport(workspace_id, binding_id, "carrier_pigeon")
