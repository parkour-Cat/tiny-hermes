"""连接的生死要留下痕迹。

断线时长是将来验证补投语义的唯一依据——拔了网线之后，要知道断了多久，
才能判断那段时间的消息有没有被补发。
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

# `_long_connections` and `_connection_event_recorder` are how `_scheduler`
# builds its connections and its audit writer — there is no public API for
# either (the plan's own Task 4 says so: "无新公开接口"), so exercising them
# means reaching into `cli.py` the same way other integration tests reach
# into a store's `_session` or a worker's private methods.
from tiny_hermes.api.cli import (
    _connection_event_recorder,  # noqa: SLF001 # pyright: ignore[reportPrivateUsage]
    _long_connections,  # noqa: SLF001 # pyright: ignore[reportPrivateUsage]
)
from tiny_hermes.channels.application.webhook_service import Claimed, Unreadable
from tiny_hermes.channels.infrastructure.feishu_long_connection import (
    FeishuLongConnection,
    LongConnectionBinding,
)
from tiny_hermes.channels.infrastructure.sql_binding_store import SqlChannelBindingStore
from tiny_hermes.secrets.domain.envelope import seal
from tiny_hermes.shared.config import Settings

from ..conftest import VALID_SPEC

#: Matches the `settings` fixture's `tiny_hermes_kek` in conftest.py
#: ("AAAA...=", 32 zero bytes decoded) — the point of sealing a secret with
#: it is exercising `_long_connections`'s real `CredentialResolver`, the
#: same one production wires, rather than a resolver built around a
#: test-only key.
KEK = b"\x00" * 32


@pytest.fixture
async def store(engine: AsyncEngine) -> AsyncIterator[SqlChannelBindingStore]:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        yield SqlChannelBindingStore(db)


async def _connection_events(store: SqlChannelBindingStore) -> list[dict[str, Any]]:
    rows = (
        await store._session.execute(  # noqa: SLF001 # pyright: ignore[reportPrivateUsage]
            text(
                "SELECT action, context FROM audit_events"
                " WHERE resource_type = 'channel_binding'"
                " AND action LIKE 'channel.long_connection.%'"
                " ORDER BY created_at, id"
            )
        )
    ).all()
    return [
        {
            "kind": str(row.action).rsplit(".", 1)[-1],
            "down_seconds": cast("dict[str, Any]", row.context or {}).get("down_seconds"),
        }
        for row in rows
    ]


async def _stored_secret(engine: AsyncEngine, workspace_id: str, value: str) -> UUID:
    """One active workspace Secret, sealed the way the service seals them.

    Reuses `test_image_source.py`'s pattern: a real KEK and a real Secret
    row, because the point of `seeded_bindings_of_both_transports` is
    exercising the path that unwraps a stored Secret, not the env-var
    branch of `CredentialResolver` that needs no store at all.
    """
    secret_id = uuid4()
    envelope = seal(value.encode("utf-8"), KEK, "test-key")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO secrets (id, name, scope, workspace_id, status, mask,"
                " ciphertext, nonce, wrapped_dek, wrap_nonce, key_id, created_at,"
                " updated_at) VALUES (:i, :name, 'workspace', :w,"
                " 'active', '****', :c, :n, :d, :dn, 'test-key', now(), now())"
            ),
            {
                "i": secret_id,
                # `secrets` enforces one name per workspace — a distinct
                # name per call so a fixture that seeds two bindings in one
                # workspace (webhook and long_connection) can give each its
                # own secret rather than colliding on a shared literal.
                "name": f"feishu-app-secret-{secret_id.hex[:8]}",
                "w": UUID(workspace_id),
                "c": envelope.ciphertext,
                "n": envelope.nonce,
                "d": envelope.wrapped_dek,
                "dn": envelope.wrap_nonce,
            },
        )
    return secret_id


def _second_agent(client: TestClient, scope: dict[str, str]) -> str:
    """A second published Agent, needed only because `channel_bindings`
    enforces one binding per `(workspace_id, channel, agent_id)` — two
    bindings in the same workspace on the same channel cannot share an
    Agent, regardless of transport.
    """
    agent_id = str(
        client.post(
            "/api/v1/agents", headers=scope, json={"name": "Second", "alias": "second"}
        ).json()["id"]
    )
    draft = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": VALID_SPEC},
    )
    assert draft.status_code == 200
    published = client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=scope,
        json={"expected_revision": draft.json()["revision"]},
    )
    assert published.status_code == 201
    return agent_id


async def _seed_binding(
    engine: AsyncEngine,
    workspace_id: str,
    agent_id: str,
    *,
    transport: str,
    app_id: str | None = None,
    app_secret_ref: str | None = None,
) -> UUID:
    binding_id = uuid4()
    async with engine.begin() as connection:
        owner = await connection.execute(text("SELECT id FROM users LIMIT 1"))
        await connection.execute(
            text(
                "INSERT INTO channel_bindings"
                " (id, workspace_id, channel, agent_id, status, created_by,"
                "  created_at, encrypt_key_ref, transport, app_id, app_secret_ref)"
                " VALUES (:i, :w, 'feishu', :a, 'active', :u, now(), 'K', :t,"
                "  :app_id, :sec)"
            ),
            {
                "i": binding_id,
                "w": UUID(workspace_id),
                "a": UUID(agent_id),
                "u": owner.scalar_one(),
                "t": transport,
                "app_id": app_id,
                "sec": app_secret_ref,
            },
        )
    return binding_id


@pytest.fixture
async def seeded_bindings_of_both_transports(
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    client: TestClient,
    scope: dict[str, str],
) -> tuple[UUID, UUID]:
    """One `webhook` binding and one `long_connection` binding with a real,
    resolvable secret.

    The webhook one has to actually exist: this fixture backs
    `test_only_long_connection_bindings_get_a_connection`, whose entire job
    is proving the filter excludes it. A fixture with only a long_connection
    binding would let that test pass for the wrong reason — see Step 5 of
    the plan, which requires removing the filter and watching this test go
    red before trusting it.

    Two different Agents, not one: `channel_bindings` enforces one binding
    per `(workspace_id, channel, agent_id)`, so two bindings on the same
    channel in the same workspace need two Agents regardless of transport.

    The webhook binding gets real, resolvable credentials too — not just a
    `transport` value. `_long_connections` also skips a binding whose
    credentials do not resolve, so a webhook binding with no `app_id`/
    `app_secret_ref` would be excluded by *that* check even with the
    transport filter deleted, and Step 5's destructive check (remove the
    filter, expect this test to go red) would pass for the wrong reason —
    exactly the "fixture proves nothing" trap the plan calls out.
    """
    webhook_secret_ref = await _stored_secret(engine, workspace_id, "webhook-secret")
    webhook_id = await _seed_binding(
        engine,
        workspace_id,
        published_agent,
        transport="webhook",
        app_id="cli_webhook",
        app_secret_ref=str(webhook_secret_ref),
    )
    second_agent = _second_agent(client, scope)
    secret_ref = await _stored_secret(engine, workspace_id, "app-secret-value")
    long_id = await _seed_binding(
        engine,
        workspace_id,
        second_agent,
        transport="long_connection",
        app_id="cli_long",
        app_secret_ref=str(secret_ref),
    )
    return webhook_id, long_id


@pytest.fixture
async def binding_with_bad_credentials(
    engine: AsyncEngine, workspace_id: str, published_agent: str
) -> UUID:
    """`app_secret_ref` names a Secret id nothing ever inserted — the
    credential-resolution failure `_long_connections` has to survive
    without raising, the same way a wrong or revoked app secret would in a
    real deployment.
    """
    return await _seed_binding(
        engine,
        workspace_id,
        published_agent,
        transport="long_connection",
        app_id="cli_bad",
        app_secret_ref=str(uuid4()),
    )


@pytest.fixture
def scheduler_connections(
    settings: Settings, engine: AsyncEngine
) -> Callable[[], Awaitable[tuple[FeishuLongConnection, ...]]]:
    """Wraps `_long_connections` — the same function `_scheduler` calls at
    startup — bound to this test's own engine rather than the real one
    `get_settings()` would build from the process environment.
    """
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def build() -> tuple[FeishuLongConnection, ...]:
        return await _long_connections(settings, sessions)

    return build


class _AdapterThatDrops:
    """Exercises `FeishuLongConnection`'s own recording methods directly,
    instead of faking a whole `FeishuChannel` socket.

    A real `reconnecting`/`reconnected` pair only fires against a live
    Feishu server. What this test needs to prove is narrower: that a
    disconnect followed by a reconnect produces exactly the two events the
    plan requires, with a real (non-negative) down time — logic that lives
    entirely in `_record_disconnected`/`_record_reconnected`, independent of
    whether the SDK or a test is what calls them.
    """

    def __init__(self, adapter: FeishuLongConnection) -> None:
        self._adapter = adapter

    async def run_until_reconnected(self) -> None:
        await self._adapter._record_disconnected()  # noqa: SLF001 # pyright: ignore[reportPrivateUsage]
        await self._adapter._record_reconnected()  # noqa: SLF001 # pyright: ignore[reportPrivateUsage]


@pytest.fixture
async def adapter_that_drops(
    engine: AsyncEngine, workspace_id: str, published_agent: str
) -> _AdapterThatDrops:
    binding_id = await _seed_binding(
        engine,
        workspace_id,
        published_agent,
        transport="long_connection",
        app_id="cli_x",
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    record = _connection_event_recorder(sessions, UUID(workspace_id))

    async def _deliver(binding_id: UUID, envelope: dict[str, Any]) -> Claimed | Unreadable:
        raise AssertionError("adapter_that_drops never delivers a frame")

    adapter = FeishuLongConnection(
        LongConnectionBinding(binding_id=binding_id, app_id="cli_x", app_secret="s"),
        _deliver,
        record=record,
    )
    return _AdapterThatDrops(adapter)


async def test_only_long_connection_bindings_get_a_connection(
    scheduler_connections: Callable[[], Awaitable[tuple[FeishuLongConnection, ...]]],
    seeded_bindings_of_both_transports: tuple[UUID, UUID],
) -> None:
    webhook_id, long_id = seeded_bindings_of_both_transports
    del webhook_id  # only the binding this test excludes, not one it asserts on

    started = await scheduler_connections()

    assert [b.binding_id for b in started] == [long_id]


async def test_a_disconnect_is_recorded_with_how_long_it_lasted(
    store: SqlChannelBindingStore, adapter_that_drops: _AdapterThatDrops
) -> None:
    await adapter_that_drops.run_until_reconnected()

    events = await _connection_events(store)
    assert [e["kind"] for e in events] == ["disconnected", "reconnected"]
    assert events[1]["down_seconds"] >= 0


async def test_a_failed_connect_does_not_stop_the_scheduler(
    scheduler_connections: Callable[[], Awaitable[tuple[FeishuLongConnection, ...]]],
    binding_with_bad_credentials: UUID,
) -> None:
    # 一个连不上的绑定不能拖垮主循环——它还负责发回复、发卡片、回执。
    started = await scheduler_connections()

    assert started is not None
