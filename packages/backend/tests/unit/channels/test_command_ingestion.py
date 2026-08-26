"""一条命令走的是另一条路：不建 Run，不进队列，但欠人一句话。"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from tiny_hermes.channels.application.ingestion import (
    ChannelBindingRecord,
    ChannelIngestion,
)
from tiny_hermes.channels.domain.events import ChannelEvent
from tiny_hermes.identity.ports.end_user_store import UpsertedIdentity
from tiny_hermes.runs.application.service import SessionBusy
from tiny_hermes.runs.domain.models import SessionMode, Withdrawal, WithdrawScope
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
    ) -> Any:
        del workspace_id, request_id
        self.images = list(images)
        self.submitted.append((end_user_id, session_id, text, idempotency_key))
        return AcceptedRun(run_id=uuid4(), document=self.document, replayed=False)

    async def withdraw_from_session(
        self, session_id: UUID, scope: WithdrawScope, *, turns: int = 1
    ) -> Withdrawal | None:
        self.withdraw_calls.append((session_id, scope, turns))
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
def busy_coordination(runs: FakeRuns) -> FakeRuns:
    """Configures the very `runs` fake `ingestion` was built with — not a
    second object — so requesting this fixture changes what withdrawal does
    inside an `ingestion` that already exists in the same test.
    """
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


async def test_a_command_does_not_become_a_run(ingestion, undo_event, binding) -> None:
    delivered = await ingestion.run_for(
        binding=binding, event=undo_event, request_id="r1"
    )

    assert delivered.run is None
    assert delivered.receipt is not None
    assert delivered.receipt.command == "undo"


async def test_an_ordinary_message_is_untouched(ingestion, text_event, binding) -> None:
    delivered = await ingestion.run_for(
        binding=binding, event=text_event, request_id="r2"
    )

    assert delivered.run is not None
    assert delivered.receipt is None


async def test_a_command_from_someone_with_no_conversation_creates_no_session(
    ingestion, undo_event, binding, conversations
) -> None:
    delivered = await ingestion.run_for(
        binding=binding, event=undo_event, request_id="r3"
    )

    assert delivered.receipt.outcome == "nothing"
    assert conversations.created == []


async def test_a_busy_session_gets_a_receipt_that_says_which_kind(
    ingestion, undo_event, binding, busy_coordination
) -> None:
    delivered = await ingestion.run_for(
        binding=binding, event=undo_event, request_id="r4"
    )

    assert delivered.receipt.outcome == "busy"
    assert delivered.receipt.busy_reason == "running"


async def test_a_finished_undo_echoes_the_withdrawn_text(
    subjects, binding, undo_event
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

    assert delivered.receipt.outcome == "done"
    assert delivered.receipt.messages == 2
    assert delivered.receipt.turns == 1
    assert delivered.receipt.echoed_text == "图里是什么"
    assert runs.withdraw_calls == [(existing_session, WithdrawScope.LAST_EXCHANGE, 1)]


async def test_new_withdraws_the_whole_session_not_just_the_last_exchange(
    subjects, binding
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

    assert delivered.receipt.command == "new"
    assert runs.withdraw_calls == [(existing_session, WithdrawScope.ALL, 1)]


async def test_a_message_the_parser_rejects_reaches_the_model_byte_identical(
    subjects, binding
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
