"""§574's deduplication, against the race it actually has to survive.

Feishu delivers at-least-once and retries on a schedule, so the same
`channel_event_id` arrives more than once — and because the retries are
driven by a queue rather than by the previous attempt finishing, two of
them can land in the same instant.

**Every test here that matters runs its two deliveries concurrently.** A
sequential duplicate is caught by a read-then-write check just as well as
by a unique constraint, so a sequential test would pass against the broken
implementation and prove nothing about the case that breaks it.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.channels.infrastructure.sql_channel_store import SqlChannelStore

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


async def _binding(engine: AsyncEngine, workspace_id: str, agent_id: str) -> UUID:
    binding_id = uuid4()
    async with engine.begin() as connection:
        user = await connection.execute(
            text("SELECT id FROM users LIMIT 1"),
        )
        await connection.execute(
            # `encrypt_key_ref` is not decoration here: migration 0037's
            # CHECK refuses a feishu binding without one, because such a
            # binding would accept unsigned deliveries from anyone who
            # learned the URL. The constraint caught this helper when it was
            # added, which is the constraint working.
            text(
                "INSERT INTO channel_bindings"
                " (id, workspace_id, channel, agent_id, status, created_by,"
                "  created_at, encrypt_key_ref)"
                " VALUES (:i, :w, 'feishu', :a, 'active', :u, :t, 'env:TEST_KEY')"
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


async def _claims(engine: AsyncEngine, binding_id: UUID) -> int:
    async with engine.connect() as connection:
        found = await connection.execute(
            text("SELECT count(*) FROM channel_events WHERE channel_binding_id = :b"),
            {"b": binding_id},
        )
        return int(found.scalar_one())


async def test_a_second_delivery_arriving_mid_transaction_claims_nothing(
    engine: AsyncEngine, workspace_id: str, published_agent: str
) -> None:
    """The interleaving a read-then-write implementation loses, forced.

    An earlier version of this test ran two deliveries through
    `asyncio.gather` and asserted one claim. It passed against a
    read-then-write implementation too — `gather` schedules coroutines, it
    does not make their database round trips overlap, and these serialized.
    A test that cannot fail against the wrong implementation is not
    evidence, so this one constructs the overlap instead of hoping for it.

    The first delivery claims and **holds its transaction open**. The second
    then runs its whole claim against a database where, under READ
    COMMITTED, the first row is not visible yet — which is exactly the state
    a `SELECT` would misread as "nobody has this". Its `INSERT` blocks on
    the unique index until the first commits, and then the index answers.

    Read-then-write raises `IntegrityError` here. `ON CONFLICT DO NOTHING`
    returns `None`, because a duplicate delivery is ordinary traffic and not
    a fault.
    """
    binding_id = await _binding(engine, workspace_id, published_agent)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    second_may_start = asyncio.Event()
    first_may_commit = asyncio.Event()

    async def first() -> UUID | None:
        async with sessions() as session:
            claimed = await SqlChannelStore(session).claim_delivery(
                binding_id, "om_the_same_event", NOW
            )
            second_may_start.set()
            await first_may_commit.wait()
            await session.commit()
            return claimed

    async def second() -> UUID | None:
        await second_may_start.wait()
        async with sessions() as session:
            claiming = asyncio.ensure_future(
                SqlChannelStore(session).claim_delivery(
                    binding_id, "om_the_same_event", NOW
                )
            )
            # The insert is now blocked on the unique index. Releasing the
            # first transaction is what lets the index decide.
            await asyncio.sleep(0.1)
            first_may_commit.set()
            claimed = await claiming
            await session.commit()
            return claimed

    outcomes = await asyncio.gather(first(), second())

    assert sum(1 for outcome in outcomes if outcome is not None) == 1
    assert await _claims(engine, binding_id) == 1


async def test_a_later_retry_of_a_settled_event_still_claims_nothing(
    engine: AsyncEngine, workspace_id: str, published_agent: str
) -> None:
    """Feishu's last retry is six hours out, long after the first delivery
    has finished and its Run exists. The claim has to keep refusing then
    too, not only during the instant two deliveries overlap."""
    binding_id = await _binding(engine, workspace_id, published_agent)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async with sessions() as session:
        store = SqlChannelStore(session)
        first = await store.claim_delivery(binding_id, "om_settled", NOW)
        assert first is not None
        await store.attach_run(first, uuid4())
        await session.commit()

    async with sessions() as session:
        again = await SqlChannelStore(session).claim_delivery(
            binding_id, "om_settled", NOW + timedelta(hours=6)
        )
        await session.commit()

    assert again is None
    assert await _claims(engine, binding_id) == 1


async def test_the_same_event_id_from_a_different_binding_is_its_own_delivery(
    engine: AsyncEngine, workspace_id: str, published_agent: str
) -> None:
    """The key is the pair (§574), not the event id alone. Two bindings are
    two subscriptions, and an id unique only within one of them must not
    silence the other."""
    first_binding = await _binding(engine, workspace_id, published_agent)
    second_binding = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO channel_bindings"
                " (id, workspace_id, channel, agent_id, status, created_by, created_at)"
                " SELECT :i, workspace_id, 'web', agent_id, 'active', created_by, :t"
                " FROM channel_bindings WHERE id = :src"
            ),
            {"i": second_binding, "t": NOW, "src": first_binding},
        )
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async with sessions() as session:
        store = SqlChannelStore(session)
        one = await store.claim_delivery(first_binding, "om_shared_id", NOW)
        other = await store.claim_delivery(second_binding, "om_shared_id", NOW)
        await session.commit()

    assert one is not None
    assert other is not None


async def test_the_sweep_forgets_past_the_window_and_keeps_what_is_inside_it(
    engine: AsyncEngine, workspace_id: str, published_agent: str
) -> None:
    """§574's seven days. `audit_events` shipped append-only with no cleanup
    and is carrying that as a recorded debt; this table does not repeat it."""
    binding_id = await _binding(engine, workspace_id, published_agent)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    cutoff = NOW - timedelta(days=7)

    async with sessions() as session:
        store = SqlChannelStore(session)
        await store.claim_delivery(binding_id, "om_ancient", cutoff - timedelta(hours=1))
        await store.claim_delivery(binding_id, "om_recent", cutoff + timedelta(hours=1))
        await session.commit()

    async with sessions() as session:
        removed = await SqlChannelStore(session).forget_deliveries_before(cutoff)
        await session.commit()

    assert removed == 1
    async with engine.connect() as connection:
        left = await connection.execute(
            text(
                "SELECT channel_event_id FROM channel_events"
                " WHERE channel_binding_id = :b"
            ),
            {"b": binding_id},
        )
        assert [row.channel_event_id for row in left] == ["om_recent"]


async def test_a_feishu_binding_without_a_key_cannot_be_created(
    engine: AsyncEngine, workspace_id: str, published_agent: str
) -> None:
    """Migration 0037's CHECK, asserted rather than assumed.

    Such a binding would accept unsigned, unencrypted deliveries from anyone
    who learned the URL — a webhook endpoint is public by construction, so
    the signature is the only thing between it and the internet. Feishu
    permits plaintext callbacks; this platform does not, and the point of
    putting that in the schema is that no code path can opt out of it.

    This constraint caught this file's own helper when it was added. That is
    the behaviour being pinned here, so a later migration cannot quietly
    relax it.
    """
    async with engine.connect() as connection:
        user = await connection.execute(text("SELECT id FROM users LIMIT 1"))
        owner = user.scalar_one()

    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO channel_bindings"
                    " (id, workspace_id, channel, agent_id, status, created_by, created_at)"
                    " VALUES (:i, :w, 'feishu', :a, 'active', :u, :t)"
                ),
                {
                    "i": uuid4(),
                    "w": UUID(workspace_id),
                    "a": UUID(published_agent),
                    "u": owner,
                    "t": NOW,
                },
            )
