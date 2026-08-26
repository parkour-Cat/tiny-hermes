"""A command produces no Run, so the reply scan never sees it.

`pending_replies` joins through `runs`; a `/undo` or `/new` starts none.
Without its own scan the sender would be told nothing — the same silence
§19.2 already forbade for a blocked queue and for an unreadable message.
This is that scan's database half: record the receipt, find it again while
it is still owed an answer, and stop finding it once it has been answered.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.channels.domain.command_receipt import CommandReceipt
from tiny_hermes.channels.infrastructure.sql_channel_store import SqlChannelStore

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


async def _binding(engine: AsyncEngine, workspace_id: str, agent_id: str) -> UUID:
    """Mirrors `test_delivery_claim._binding` — a feishu binding needs
    `encrypt_key_ref` to satisfy migration 0037's CHECK."""
    binding_id = uuid4()
    async with engine.begin() as connection:
        user = await connection.execute(text("SELECT id FROM users LIMIT 1"))
        await connection.execute(
            text(
                "INSERT INTO channel_bindings"
                " (id, workspace_id, channel, agent_id, status, created_by,"
                "  created_at, encrypt_key_ref)"
                " VALUES (:i, :w, 'feishu', :a, 'active', :u, :t, 'TEST_KEY')"
            ),
            {
                "i": binding_id,
                "w": UUID(workspace_id),
                "a": UUID(agent_id),
                "u": user.scalar_one(),
                "t": NOW,
            },
        )
    return binding_id


@dataclass(frozen=True)
class _ClaimedEvent:
    """The one fact both tests need: a `channel_events` row that exists and
    holds no `run_id` — a command claims a delivery the same way any other
    message does, and then never attaches a Run to it."""

    id: UUID
    binding_id: UUID


@pytest.fixture
async def channel_store(engine: AsyncEngine) -> AsyncIterator[SqlChannelStore]:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        yield SqlChannelStore(session)
        await session.commit()


@pytest.fixture
async def claimed_event(
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    channel_store: SqlChannelStore,
) -> _ClaimedEvent:
    binding_id = await _binding(engine, workspace_id, published_agent)
    event_row_id = await channel_store.claim_delivery(binding_id, "om_command_1", NOW)
    assert event_row_id is not None
    return _ClaimedEvent(id=event_row_id, binding_id=binding_id)


async def test_a_recorded_receipt_is_owed_an_answer(
    channel_store: SqlChannelStore, claimed_event: _ClaimedEvent
) -> None:
    await channel_store.record_command_receipt(
        claimed_event.id,
        CommandReceipt("undo", "done", 2, 1, "图里是什么", None),
        external_user_id="ou_test",
    )

    pending = await channel_store.pending_command_receipts()

    assert [p.event_row_id for p in pending] == [claimed_event.id]
    assert pending[0].receipt.messages == 2
    assert pending[0].binding_id == claimed_event.binding_id
    assert pending[0].external_user_id == "ou_test"


async def test_an_answered_receipt_is_not_owed_again(
    channel_store: SqlChannelStore, claimed_event: _ClaimedEvent
) -> None:
    await channel_store.record_command_receipt(
        claimed_event.id,
        CommandReceipt("undo", "done", 2, 1, "图里是什么", None),
        external_user_id="ou_test",
    )
    await channel_store.settle_reply(claimed_event.id, note="ok", now=NOW)

    assert await channel_store.pending_command_receipts() == []
