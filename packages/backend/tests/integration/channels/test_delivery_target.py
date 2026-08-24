"""Where a finished Run's result should go, read back from its session.

Inbound turns a Feishu message into a Run and forgets the sender. Outbound
has the opposite problem: a Run finishes carrying only a `session_id`, and
something has to learn that this session belongs to a Feishu conversation —
and to whom — before it can reply. `delivery_target_for` is that lookup,
and it is pure database, so it is worth pinning even though the send it
feeds cannot be verified without a tenant.
"""

from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.channels.infrastructure.sql_channel_store import SqlChannelStore


async def _binding(
    engine: AsyncEngine,
    workspace_id: str,
    agent_id: str,
    *,
    # noqa on the next line: this is the *name* of a secret, never a secret.
    # That distinction is the whole point of the `_ref` columns (migration
    # 0037), so a rule that reads it as a password is reading it backwards.
    app_secret_ref: str | None = "feishu-app-secret",  # noqa: S107
    status: str = "active",
) -> UUID:
    binding_id = uuid4()
    async with engine.begin() as connection:
        user = await connection.execute(text("SELECT id FROM users LIMIT 1"))
        await connection.execute(
            text(
                "INSERT INTO channel_bindings"
                " (id, workspace_id, channel, agent_id, status, created_by,"
                "  created_at, encrypt_key_ref, app_id, app_secret_ref)"
                " VALUES (:i, :w, 'feishu', :a, :s, :u, now(), 'K', 'cli_x', :sec)"
            ),
            {
                "i": binding_id,
                "w": UUID(workspace_id),
                "a": UUID(agent_id),
                "s": status,
                "u": user.scalar_one(),
                "sec": app_secret_ref,
            },
        )
    return binding_id


async def _session_on(
    engine: AsyncEngine, workspace_id: str, agent_id: str
) -> UUID:
    session_id = uuid4()
    subject = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO end_users (id, workspace_id, created_at)"
                " VALUES (:i, :w, now())"
            ),
            {"i": subject, "w": UUID(workspace_id)},
        )
        await connection.execute(
            text(
                "INSERT INTO sessions"
                " (id, workspace_id, agent_id, session_mode, caller_type, caller_id,"
                "  next_run_sequence, next_message_sequence, created_at)"
                " VALUES (:i, :w, :a, 'persistent', 'end_user', :c, 1, 1, now())"
            ),
            {"i": session_id, "w": UUID(workspace_id), "a": UUID(agent_id), "c": subject},
        )
    return session_id


async def _remember(
    engine: AsyncEngine, binding_id: UUID, external_user_id: str, session_id: UUID
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO channel_conversations"
                " (id, channel_binding_id, external_user_id, session_id, created_at)"
                " VALUES (gen_random_uuid(), :b, :e, :s, now())"
            ),
            {"b": binding_id, "e": external_user_id, "s": session_id},
        )


async def test_a_channel_session_resolves_to_its_participant_and_credentials(
    engine: AsyncEngine, workspace_id: str, published_agent: str
) -> None:
    binding_id = await _binding(engine, workspace_id, published_agent)
    session_id = await _session_on(engine, workspace_id, published_agent)
    await _remember(engine, binding_id, "ou_alice", session_id)

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        target = await SqlChannelStore(db).delivery_target_for(session_id)

    assert target is not None
    assert target.binding_id == binding_id
    assert target.external_user_id == "ou_alice"
    assert target.app_secret_ref == "feishu-app-secret"  # noqa: S105 - a name
    assert target.binding_active is True


async def test_an_ordinary_session_resolves_to_nothing(
    engine: AsyncEngine, workspace_id: str, published_agent: str
) -> None:
    """A console Run's result is read on the web, not pushed to a channel.
    `None` is that, not a failure — and it is the common case."""
    session_id = await _session_on(engine, workspace_id, published_agent)

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        target = await SqlChannelStore(db).delivery_target_for(session_id)

    assert target is None


async def test_a_receive_only_binding_resolves_with_no_secret(
    engine: AsyncEngine, workspace_id: str, published_agent: str
) -> None:
    """§929's drill binding: it maps a session but replies to nobody. The
    lookup returns the target with `app_secret_ref` None, and the consumer
    reads that as 'nowhere to reply'."""
    binding_id = await _binding(engine, workspace_id, published_agent, app_secret_ref=None)
    session_id = await _session_on(engine, workspace_id, published_agent)
    await _remember(engine, binding_id, "ou_bob", session_id)

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        target = await SqlChannelStore(db).delivery_target_for(session_id)

    assert target is not None
    assert target.app_secret_ref is None


async def test_a_disabled_binding_is_reported_as_shut(
    engine: AsyncEngine, workspace_id: str, published_agent: str
) -> None:
    """A disabled binding still maps the session — the history stays — but
    the channel is closed, and the consumer must not reply through a door an
    administrator shut."""
    binding_id = await _binding(engine, workspace_id, published_agent, status="disabled")
    session_id = await _session_on(engine, workspace_id, published_agent)
    await _remember(engine, binding_id, "ou_carol", session_id)

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        target = await SqlChannelStore(db).delivery_target_for(session_id)

    assert target is not None
    assert target.binding_active is False


async def test_the_lookup_answers_for_the_session_it_was_asked_about(
    engine: AsyncEngine, workspace_id: str, published_agent: str
) -> None:
    """Two conversations, and the query must pick the right one.

    Written after the fact, because the four tests above did not catch this:
    each seeded exactly one conversation, so a query that dropped its
    `WHERE session_id = …` entirely still passed all four. Removing that
    clause is the falsification this test exists to fail — one row in the
    table makes "found the right one" and "found the only one"
    indistinguishable, which is the shape of test this repository keeps
    finding.
    """
    # One binding, two participants — a Feishu app serves many people, and
    # `uq_channel_bindings_target` allows only one binding per
    # (workspace, channel, agent) anyway. The constraint caught the first
    # version of this test, which is the constraint working.
    binding_id = await _binding(engine, workspace_id, published_agent)
    alice_session = await _session_on(engine, workspace_id, published_agent)
    bob_session = await _session_on(engine, workspace_id, published_agent)
    await _remember(engine, binding_id, "ou_alice", alice_session)
    await _remember(engine, binding_id, "ou_bob", bob_session)

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        store = SqlChannelStore(db)
        for_alice = await store.delivery_target_for(alice_session)
        for_bob = await store.delivery_target_for(bob_session)

    assert for_alice is not None
    assert for_bob is not None
    assert for_alice.external_user_id == "ou_alice"
    assert for_bob.external_user_id == "ou_bob"
