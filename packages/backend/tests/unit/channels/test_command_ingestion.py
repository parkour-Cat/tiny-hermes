"""一条命令走的是另一条路：不建 Run，不进队列，但欠人一句话。"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from tiny_hermes.channels.application.ingestion import (
    ChannelBindingRecord,
    ChannelIngestion,
)
from tiny_hermes.channels.domain.events import ChannelEvent
from tiny_hermes.identity.ports.end_user_store import UpsertedIdentity
from tiny_hermes.runs.application.service import IdempotencyKeyRequired, SessionBusy
from tiny_hermes.runs.domain.models import (
    EndUserEscape,
    RunPurpose,
    SessionMode,
    Withdrawal,
    WithdrawScope,
)
from tiny_hermes.runs.ports.store import AcceptedRun


class FakeSubjects:
    def __init__(self, erased_at: datetime | None = None) -> None:
        self.end_user_id = uuid4()
        self.erased_at = erased_at

    async def upsert_external_identity(
        self, workspace_id: UUID, channel: str, external_user_id: str
    ) -> UpsertedIdentity:
        del workspace_id, channel, external_user_id
        return UpsertedIdentity(end_user_id=self.end_user_id, erased_at=self.erased_at)


class FakeConversations:
    """`created` tracks `remember_session` — the call that actually makes a
    `channel_conversations` row exist. `session_for` returning `None` is not
    by itself "created"; it is what a command's caller checks *before*
    deciding whether to create anything.
    """

    def __init__(self, known: UUID | None = None) -> None:
        self.known = known
        self.created: list[tuple[UUID, str, UUID]] = []

    async def session_for(self, binding_id: UUID, external_user_id: str) -> UUID | None:
        del binding_id, external_user_id
        return self.known

    async def remember_session(
        self, binding_id: UUID, external_user_id: str, session_id: UUID
    ) -> None:
        self.created.append((binding_id, external_user_id, session_id))


class FakeRuns:
    """Plays both `RunEntry` roles this test needs: submitting an ordinary
    message, and withdrawing on behalf of a command. One fake rather than
    two, because `ChannelIngestion` is wired to a single `RunCoordination`
    in production — splitting it here would test a shape nothing runs.
    """

    def __init__(
        self,
        document: dict[str, Any] | None = None,
        withdrawal: Withdrawal | None = None,
        busy_reason: str | None = None,
    ) -> None:
        self.document: dict[str, Any] = document if document is not None else {}
        self.created: list[tuple[UUID, UUID, SessionMode]] = []
        #: `text` is kept (not discarded like the other unit-checked
        #: arguments) because one test in this file exists solely to prove
        #: it arrives byte-identical — that is the whole claim this task
        #: makes true or false.
        self.submitted: list[tuple[UUID, UUID, str, str | None]] = []
        self.withdraw_calls: list[tuple[UUID, WithdrawScope, int]] = []
        self.compaction_requests: list[UUID] = []
        self.purposes: list[Any] = []
        self.compactable = True
        #: 每次撤回带的逃生口（`/new` 有，`/undo` 必须没有）。
        self.escapes: list[EndUserEscape | None] = []
        self.session_id = uuid4()
        self.withdrawal = withdrawal
        self.busy_reason = busy_reason
        self.images: list[Any] = []

    async def create_end_user_session(
        self,
        workspace_id: UUID,
        end_user_id: UUID,
        agent_id: UUID,
        session_mode: SessionMode,
        request_id: str,
    ) -> Any:
        del request_id, workspace_id
        self.created.append((end_user_id, agent_id, session_mode))

        class Snapshot:
            id = self.session_id

        return Snapshot()

    async def submit_end_user_run(
        self,
        workspace_id: UUID,
        end_user_id: UUID,
        session_id: UUID,
        text: str,
        idempotency_key: str | None,
        request_id: str,
        images: Sequence[Any] = (),
        purpose: Any = None,
    ) -> Any:
        del workspace_id, request_id
        # 真的那一边会拒。`RunCoordination.submit_end_user_run` 第一句就是
        # `_require_idempotency_key(idempotency_key)`，空的直接抛。这个假的
        # 以前照单全收，于是 `/compact` 传了 `None` 也一路绿——线上第一条
        # `/compact` 就在这里炸了 `IdempotencyKeyRequired`，而后端全绿。
        #
        # 这就是 CLAUDE.md 说的那件事：夹具比被集成的那一方宽松，证明的是
        # 「我们以为对面收什么」，不是「对面实际收什么」。
        if not (idempotency_key or "").strip():
            raise IdempotencyKeyRequired
        self.images = list(images)
        self.purposes.append(purpose)
        self.submitted.append((end_user_id, session_id, text, idempotency_key))
        return AcceptedRun(run_id=uuid4(), document=self.document, replayed=False)

    async def request_compaction(self, session_id: UUID) -> bool:
        self.compaction_requests.append(session_id)
        return self.compactable

    async def withdraw_from_session(
        self,
        session_id: UUID,
        scope: WithdrawScope,
        *,
        turns: int = 1,
        escape_hatch: EndUserEscape | None = None,
    ) -> Withdrawal | None:
        self.withdraw_calls.append((session_id, scope, turns))
        self.escapes.append(escape_hatch)
        if self.busy_reason is not None:
            raise SessionBusy(self.busy_reason)
        return self.withdrawal


BINDING = ChannelBindingRecord(
    id=uuid4(), workspace_id=uuid4(), agent_id=uuid4(), channel="feishu"
)


@pytest.fixture
def binding() -> ChannelBindingRecord:
    return BINDING


@pytest.fixture
def undo_event() -> ChannelEvent:
    return ChannelEvent(
        channel="feishu",
        channel_event_id="om_undo",
        external_user_id="ou_zhang",
        text="/undo",
    )


@pytest.fixture
def text_event() -> ChannelEvent:
    return ChannelEvent(
        channel="feishu",
        channel_event_id="om_text",
        external_user_id="ou_zhang",
        text="帮我查一下上周的订单",
    )


@pytest.fixture
def subjects() -> FakeSubjects:
    return FakeSubjects()


@pytest.fixture
def conversations() -> FakeConversations:
    # No known session by default: the whole point of
    # `test_a_command_from_someone_with_no_conversation_creates_no_session`
    # is that a command's *first* message from someone must not create one.
    return FakeConversations(known=None)


@pytest.fixture
def runs() -> FakeRuns:
    return FakeRuns()


@pytest.fixture
def busy_coordination(runs: FakeRuns, conversations: FakeConversations) -> FakeRuns:
    """Configures the very `runs` and `conversations` fakes `ingestion` was
    built with — not second objects — so requesting this fixture changes
    what an already-built `ingestion` does in the same test. A session must
    exist for `withdraw_from_session` to be reached at all (see
    `_command`'s "no conversation" short-circuit), so this also gives
    `conversations` one; the default fixture deliberately has none.
    """
    conversations.known = uuid4()
    runs.busy_reason = "running"
    return runs


@pytest.fixture
def ingestion(
    subjects: FakeSubjects, conversations: FakeConversations, runs: FakeRuns
) -> ChannelIngestion:
    return ChannelIngestion(
        subjects=subjects,  # pyright: ignore[reportArgumentType]
        conversations=conversations,  # pyright: ignore[reportArgumentType]
        runs=runs,  # pyright: ignore[reportArgumentType]
    )


async def test_a_command_does_not_become_a_run(
    ingestion: ChannelIngestion, undo_event: ChannelEvent, binding: ChannelBindingRecord
) -> None:
    delivered = await ingestion.run_for(
        binding=binding, event=undo_event, request_id="r1"
    )

    assert delivered.run is None
    assert delivered.receipt is not None
    assert delivered.receipt.command == "undo"


async def test_an_ordinary_message_is_untouched(
    ingestion: ChannelIngestion, text_event: ChannelEvent, binding: ChannelBindingRecord
) -> None:
    delivered = await ingestion.run_for(
        binding=binding, event=text_event, request_id="r2"
    )

    assert delivered.run is not None
    assert delivered.receipt is None


async def test_a_command_from_someone_with_no_conversation_creates_no_session(
    ingestion: ChannelIngestion,
    undo_event: ChannelEvent,
    binding: ChannelBindingRecord,
    conversations: FakeConversations,
) -> None:
    delivered = await ingestion.run_for(
        binding=binding, event=undo_event, request_id="r3"
    )

    assert delivered.receipt is not None
    assert delivered.receipt.outcome == "nothing"
    assert conversations.created == []


async def test_a_busy_session_gets_a_receipt_that_says_which_kind(
    ingestion: ChannelIngestion,
    undo_event: ChannelEvent,
    binding: ChannelBindingRecord,
    busy_coordination: FakeRuns,
) -> None:
    del busy_coordination
    delivered = await ingestion.run_for(
        binding=binding, event=undo_event, request_id="r4"
    )

    assert delivered.receipt is not None
    assert delivered.receipt.outcome == "busy"
    assert delivered.receipt.busy_reason == "running"


async def test_a_finished_undo_echoes_the_withdrawn_text(
    subjects: FakeSubjects, binding: ChannelBindingRecord, undo_event: ChannelEvent
) -> None:
    """The receipt's numbers must be what actually happened, not what was
    asked for — mirrors `Withdrawal`'s own contract (`turns` is the real
    count, not the requested one) one layer up, at the point a person reads
    it back.
    """
    existing_session = uuid4()
    conversations = FakeConversations(known=existing_session)
    runs = FakeRuns(
        withdrawal=Withdrawal(messages=2, turns=1, echoed_text="图里是什么")
    )
    ingestion = ChannelIngestion(
        subjects=subjects,  # pyright: ignore[reportArgumentType]
        conversations=conversations,  # pyright: ignore[reportArgumentType]
        runs=runs,  # pyright: ignore[reportArgumentType]
    )

    delivered = await ingestion.run_for(
        binding=binding, event=undo_event, request_id="r5"
    )

    assert delivered.receipt is not None
    assert delivered.receipt.outcome == "done"
    assert delivered.receipt.messages == 2
    assert delivered.receipt.turns == 1
    assert delivered.receipt.echoed_text == "图里是什么"
    assert runs.withdraw_calls == [(existing_session, WithdrawScope.LAST_EXCHANGE, 1)]


async def test_new_withdraws_the_whole_session_not_just_the_last_exchange(
    subjects: FakeSubjects, binding: ChannelBindingRecord
) -> None:
    """`/new` is "draw a line across this session", not `/undo` with a
    bigger number — §8's decision that it stays one Session entity. Scope
    is how that decision is expressed to `RunCoordination`.
    """
    existing_session = uuid4()
    conversations = FakeConversations(known=existing_session)
    runs = FakeRuns(withdrawal=Withdrawal(messages=6, turns=3, echoed_text=""))
    ingestion = ChannelIngestion(
        subjects=subjects,  # pyright: ignore[reportArgumentType]
        conversations=conversations,  # pyright: ignore[reportArgumentType]
        runs=runs,  # pyright: ignore[reportArgumentType]
    )
    new_event = ChannelEvent(
        channel="feishu",
        channel_event_id="om_new",
        external_user_id="ou_zhang",
        text="/new",
    )

    delivered = await ingestion.run_for(
        binding=binding, event=new_event, request_id="r6"
    )

    assert delivered.receipt is not None
    assert delivered.receipt.command == "new"
    assert runs.withdraw_calls == [(existing_session, WithdrawScope.ALL, 1)]


async def test_a_message_the_parser_rejects_reaches_the_model_byte_identical(
    subjects: FakeSubjects, binding: ChannelBindingRecord
) -> None:
    """The claim this task exists to make true: `commands.py`'s docstring
    says a non-command message beginning with `/` is passed through to the
    model untouched. Before this task nothing called `parse` at all, so
    that sentence was aspirational. This is the seam that decides whether
    the text on its way to `submit_end_user_run` is exactly what arrived.
    """
    existing_session = uuid4()
    conversations = FakeConversations(known=existing_session)
    runs = FakeRuns()
    ingestion = ChannelIngestion(
        subjects=subjects,  # pyright: ignore[reportArgumentType]
        conversations=conversations,  # pyright: ignore[reportArgumentType]
        runs=runs,  # pyright: ignore[reportArgumentType]
    )
    original_text = "/usr/local/bin/foo --help"
    slash_path = ChannelEvent(
        channel="feishu",
        channel_event_id="om_path",
        external_user_id="ou_zhang",
        text=original_text,
    )

    delivered = await ingestion.run_for(
        binding=binding, event=slash_path, request_id="r7"
    )

    assert delivered.run is not None
    assert delivered.receipt is None
    # Byte-identical: not stripped, not truncated, not re-encoded. `parse`
    # rejected it (more than one word, first word not a known command name),
    # so it must fall through to the same `submit_end_user_run` call an
    # ordinary message takes.
    assert runs.submitted == [
        (subjects.end_user_id, existing_session, original_text, "om_path")
    ]


async def test_only_new_carries_the_permission_to_end_a_parked_run(
    subjects: FakeSubjects, binding: ChannelBindingRecord
) -> None:
    """阻塞卡片对同一个人说「被卡住时，可以发 /new 开始一段新对话」。让那句话
    成立的就是这个逃生口——它必须跟着 `/new` 走到服务层。

    `/undo` 必须**不**带它：`/undo` 是对已经落定的历史动刀，没有理由替用户
    放弃一个他没说要放弃的 Run。两个断言写在一起，因为这个不对称本身才是被
    钉住的东西——只断言 `/new` 带了，一个两条命令都带的实现照样能过。
    """
    existing_session = uuid4()
    runs = FakeRuns(withdrawal=Withdrawal(messages=2, turns=1, echoed_text="在的"))
    ingestion = ChannelIngestion(
        subjects=subjects,  # pyright: ignore[reportArgumentType]
        conversations=FakeConversations(known=existing_session),  # pyright: ignore[reportArgumentType]
        runs=runs,  # pyright: ignore[reportArgumentType]
    )
    new_event = ChannelEvent(
        channel="feishu",
        channel_event_id="om_new",
        external_user_id="ou_zhang",
        text="/new",
    )

    await ingestion.run_for(binding=binding, event=new_event, request_id="r-new")
    await ingestion.run_for(
        binding=binding,
        event=ChannelEvent(
            channel="feishu",
            channel_event_id="om_undo",
            external_user_id="ou_zhang",
            text="/undo",
        ),
        request_id="r-undo",
    )

    for_new, for_undo = runs.escapes
    assert for_new == EndUserEscape(
        workspace_id=binding.workspace_id,
        end_user_id=subjects.end_user_id,
        request_id="r-new",
    )
    assert for_undo is None


@pytest.fixture
def compact_event() -> ChannelEvent:
    return ChannelEvent(
        channel="feishu",
        channel_event_id="om_compact",
        external_user_id="ou_zhang",
        text="/compact",
    )


async def test_compact_marks_the_session_and_starts_a_compaction_run(
    ingestion: ChannelIngestion,
    compact_event: ChannelEvent,
    binding: ChannelBindingRecord,
    runs: FakeRuns,
    conversations: FakeConversations,
) -> None:
    """打标记，**并且**提交一个只做压缩的 Run。

    `/compact` 是唯一一条会产生 Run 的命令，这推翻了 `Delivered` 原来那条
    「`run` 是 `None` 恰好当 `receipt` 不是」。理由是记账：摘要要花钱，而这个
    平台里的钱必须挂在一个 Run 上。当初立那条规矩的理由是「命令是纯数据操作、
    不花钱」——对这一条本来就不成立。

    断言三件事一起：标记打了、Run 是 `COMPACTION` 那种、**回执这一刻不发**。
    最后一条是重点：压完之后才知道省了多少，那时才有话可说；现在就发一句话，
    只能是句空话。
    """
    # 默认 fixture 故意没有会话（那条守的是「陌生人的第一条消息不建会话」）。
    # 不给一个，这条测试测到的是那条早退路径，压缩这段代码根本走不到。
    conversations.known = runs.session_id

    delivered = await ingestion.run_for(
        binding=binding, event=compact_event, request_id="r1"
    )

    assert runs.compaction_requests == [runs.session_id]
    # 提交了一个**只做压缩**的 Run。这是 `/compact` 与其它命令的唯一区别，
    # 理由是记账：摘要是一次真实的模型调用，而这个平台里的钱必须挂在 Run 上。
    assert len(runs.submitted) == 1
    assert runs.purposes == [RunPurpose.COMPACTION]
    # 幂等键是这条飞书消息的 id，和普通消息那条路一模一样。钉住具体的值而不是
    # 「非空」：飞书会重投同一条消息（§19.2），而重投必须换回同一个 Run，
    # 不是第二个花钱的压缩。
    assert runs.submitted[0][-1] == compact_event.channel_event_id
    assert delivered.run is not None
    # 仍然不是撤回——压缩不动历史里的任何一条消息。
    assert runs.withdraw_calls == []
    # 回执不在命令这一刻发：压完之后才知道省了多少，那时才有话可说。
    assert delivered.receipt is None


async def test_compact_on_a_conversation_with_nothing_to_compact_says_nothing(
    ingestion: ChannelIngestion,
    compact_event: ChannelEvent,
    binding: ChannelBindingRecord,
    runs: FakeRuns,
    conversations: FakeConversations,
) -> None:
    """短对话里没有可压缩的历史，回执要说这件事，不能报「已记下」。

    `outcome` 由 store 说了算（它才知道这段会话有多长），不是这一层猜的。
    """
    conversations.known = runs.session_id
    runs.compactable = False

    delivered = await ingestion.run_for(
        binding=binding, event=compact_event, request_id="r1"
    )

    assert delivered.receipt is not None
    assert delivered.receipt.outcome == "nothing"
