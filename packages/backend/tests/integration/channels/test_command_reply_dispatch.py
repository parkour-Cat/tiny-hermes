"""这条是本项目最常见 bug 的直接防线：写进去了不等于有人够得着。

`test_command_receipt_scan.py` 已经证明 `pending_command_receipts` 能找到一条
欠着回执的事件——那是查询本身的正确性。它没有证明、也不打算证明的是：
`ChannelReplyDispatcher` 真的会去读这个查询、把回执渲染成一句话、并且真的调用
了发送方。这个模块之前只有 `pending_replies`/`pending_refusals`/
`pending_blocked_notices` 三条分支；命令回执要有自己的第四条,否则它和
`failure_reason`、`forgetAllSessionIds`一样，是写进数据库、永远没有代码路径
读它的东西。

所以这里不摆事件行,而是驱动真正的 `ChannelReplyDispatcher.dispatch_once()`,
用一个记录发送内容的假发送方断言「文字真的被发出去了」——不是回执行存在,
不是渲染函数返回了字符串。
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tiny_hermes.channels.application.outbound import ChannelReplyDispatcher
from tiny_hermes.channels.domain.command_receipt import CommandReceipt
from tiny_hermes.channels.infrastructure.sql_channel_store import SqlChannelStore
from tiny_hermes.model_catalog.infrastructure.credentials import CredentialResolver
from tiny_hermes.secrets.infrastructure.sql_store import SqlSecretStore

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)

#: The *name* of an environment variable, never a secret itself — the same
#: distinction the `_ref` columns draw everywhere else in this module.
SECRET_ENV = "FEISHU_COMMAND_REPLY_SECRET_FOR_TEST"  # noqa: S105


class _SpySender:
    """Records every text send and nothing else.

    Card methods raise rather than silently recording: a command receipt has
    no Run and no opening card, so if the dispatcher ever routed one through
    `send_card`/`update_card` that would be a wiring bug worth failing loudly
    on, not a call this fake should absorb quietly.
    """

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.asked_for: list[UUID] = []

    def __call__(self, workspace_id: UUID) -> "_SpySender":
        self.asked_for.append(workspace_id)
        return self

    async def send_text(
        self,
        *,
        app_id: str,
        app_secret: str,
        open_id: str,
        text: str,
        delivery_key: str | None = None,
    ) -> None:
        self.texts.append(text)

    async def send_card(
        self,
        *,
        app_id: str,
        app_secret: str,
        open_id: str,
        card: dict[str, Any],
        delivery_key: str | None = None,
    ) -> str | None:
        raise AssertionError("a command receipt is answered with text, not a card")

    async def update_card(
        self,
        *,
        app_id: str,
        app_secret: str,
        message_id: str,
        card: dict[str, Any],
    ) -> None:
        raise AssertionError("a command receipt is answered with text, not a card")


@dataclass(frozen=True)
class _ClaimedEvent:
    """Mirrors `test_command_receipt_scan._ClaimedEvent`: a `channel_events`
    row that exists and holds no `run_id`, because a command never starts one."""

    id: UUID
    binding_id: UUID


async def _binding(engine: AsyncEngine, workspace_id: str, agent_id: str) -> UUID:
    """A binding with real send credentials.

    `test_command_receipt_scan._binding` deliberately leaves `app_secret_ref`
    unset — that suite is about the query, not about sending. This one is
    about sending, so the binding needs everything `binding_target` and the
    dispatcher's credential resolution actually require.
    """
    binding_id = uuid4()
    async with engine.begin() as connection:
        owner = await connection.execute(text("SELECT id FROM users LIMIT 1"))
        await connection.execute(
            text(
                "INSERT INTO channel_bindings"
                " (id, workspace_id, channel, agent_id, status, created_by,"
                "  created_at, encrypt_key_ref, app_id, app_secret_ref)"
                " VALUES (:i, :w, 'feishu', :a, 'active', :u, :t,"
                "         'TEST_KEY', 'cli_x', :sec)"
            ),
            {
                "i": binding_id,
                "w": UUID(workspace_id),
                "a": UUID(agent_id),
                "u": owner.scalar_one(),
                "t": NOW,
                "sec": SECRET_ENV,
            },
        )
    return binding_id


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """One session for the whole test: the write and the dispatcher's read of
    it must share a transaction, or the read would see nothing yet to find —
    `channel_store`'s own commit happens at teardown, after the test body."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        yield session
        await session.commit()


@pytest.fixture
def channel_store(db_session: AsyncSession) -> SqlChannelStore:
    return SqlChannelStore(db_session)


@pytest.fixture
def sender_spy() -> _SpySender:
    return _SpySender()


@pytest.fixture
def outbound(
    db_session: AsyncSession,
    sender_spy: _SpySender,
    monkeypatch: pytest.MonkeyPatch,
) -> ChannelReplyDispatcher:
    """The real dispatcher, reading the real query, wired to the spy sender.

    A second `SqlChannelStore` over the same `db_session` as `channel_store`
    — two stores, one transaction, so `record_command_receipt` and
    `dispatch_once` see the same uncommitted rows.
    """
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    resolver = CredentialResolver(SqlSecretStore(db_session), None)
    return ChannelReplyDispatcher(
        store=SqlChannelStore(db_session),
        resolve_secret=resolver.resolve,
        senders=sender_spy,
    )


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


async def test_the_receipt_actually_reaches_the_sender(
    outbound: ChannelReplyDispatcher,
    channel_store: SqlChannelStore,
    claimed_event: _ClaimedEvent,
    sender_spy: _SpySender,
) -> None:
    await channel_store.record_command_receipt(
        claimed_event.id,
        CommandReceipt("undo", "done", 2, 1, "图里是什么", None),
        external_user_id="ou_test",
    )

    await outbound.dispatch_once()

    assert sender_spy.texts, "回执被记录了，但没有任何东西把它发出去"
    assert "图里是什么" in sender_spy.texts[0]


async def test_the_receipt_is_settled_so_it_is_not_sent_twice(
    outbound: ChannelReplyDispatcher,
    channel_store: SqlChannelStore,
    claimed_event: _ClaimedEvent,
    sender_spy: _SpySender,
) -> None:
    """The scan's predicate is `replied_at IS NULL`; settling the row is the
    only thing that stops a second pass from sending the same receipt again."""
    await channel_store.record_command_receipt(
        claimed_event.id,
        CommandReceipt("undo", "done", 2, 1, "图里是什么", None),
        external_user_id="ou_test",
    )

    await outbound.dispatch_once()
    await outbound.dispatch_once()

    assert len(sender_spy.texts) == 1


async def test_a_busy_receipt_reaches_the_sender_too(
    outbound: ChannelReplyDispatcher,
    channel_store: SqlChannelStore,
    claimed_event: _ClaimedEvent,
    sender_spy: _SpySender,
) -> None:
    await channel_store.record_command_receipt(
        claimed_event.id,
        CommandReceipt("undo", "busy", 0, 0, "", "running"),
        external_user_id="ou_test",
    )

    await outbound.dispatch_once()

    assert sender_spy.texts
