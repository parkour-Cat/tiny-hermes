"""A Feishu message becomes a Run as the person who sent it — and does not
become one when that person has been erased.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from tiny_hermes.channels.application.ingestion import (
    ChannelBindingRecord,
    ChannelIngestion,
    ErasedSubjectRefused,
)
from tiny_hermes.channels.domain.events import ChannelEvent
from tiny_hermes.identity.ports.end_user_store import UpsertedIdentity
from tiny_hermes.runs.domain.models import SessionMode
from tiny_hermes.runs.ports.store import AcceptedRun

BINDING = ChannelBindingRecord(
    id=uuid4(), workspace_id=uuid4(), agent_id=uuid4(), channel="feishu"
)
EVENT = ChannelEvent(
    channel="feishu",
    channel_event_id="om_1",
    external_user_id="ou_zhang",
    text="帮我查一下上周的订单",
)


class FakeSubjects:
    def __init__(self, erased_at: datetime | None = None) -> None:
        self.end_user_id = uuid4()
        self.erased_at = erased_at
        self.calls: list[tuple[UUID, str, str]] = []

    async def upsert_external_identity(
        self, workspace_id: UUID, channel: str, external_user_id: str
    ) -> UpsertedIdentity:
        self.calls.append((workspace_id, channel, external_user_id))
        return UpsertedIdentity(end_user_id=self.end_user_id, erased_at=self.erased_at)


class FakeConversations:
    def __init__(self, known: UUID | None = None) -> None:
        self.known = known
        self.remembered: list[tuple[UUID, str, UUID]] = []

    async def session_for(self, binding_id: UUID, external_user_id: str) -> UUID | None:
        del binding_id, external_user_id
        return self.known

    async def remember_session(
        self, binding_id: UUID, external_user_id: str, session_id: UUID
    ) -> None:
        self.remembered.append((binding_id, external_user_id, session_id))


class FakeRuns:
    def __init__(self, document: dict[str, Any] | None = None) -> None:
        self.document: dict[str, Any] = document if document is not None else {}
        self.created: list[tuple[UUID, UUID, SessionMode]] = []
        self.submitted: list[tuple[UUID, UUID, str | None]] = []
        self.session_id = uuid4()

    async def create_end_user_session(
        self,
        workspace_id: UUID,
        end_user_id: UUID,
        agent_id: UUID,
        session_mode: SessionMode,
        request_id: str,
    ) -> Any:
        del request_id
        self.created.append((end_user_id, agent_id, session_mode))

        class Snapshot:
            id = self.session_id

        del workspace_id
        return Snapshot()

    async def submit_end_user_run(
        self,
        workspace_id: UUID,
        end_user_id: UUID,
        session_id: UUID,
        text: str,
        idempotency_key: str | None,
        request_id: str,
    ) -> Any:
        del workspace_id, text, request_id
        self.submitted.append((end_user_id, session_id, idempotency_key))
        return AcceptedRun(run_id=uuid4(), document=self.document, replayed=False)


def _ingestion(
    subjects: FakeSubjects, conversations: FakeConversations, runs: FakeRuns
) -> ChannelIngestion:
    return ChannelIngestion(
        subjects=subjects,  # pyright: ignore[reportArgumentType]
        conversations=conversations,  # pyright: ignore[reportArgumentType]
        runs=runs,  # pyright: ignore[reportArgumentType]
    )


async def test_an_erased_subject_cannot_come_back_through_feishu() -> None:
    """§344 has to hold across transports or it does not hold.

    The web path refuses an erased subject at credential exchange, and that
    check lives in the service rather than in `upsert_external_identity` —
    so a transport that forgot it would let the same person walk in through
    a different door and be resurrected. No Run, and no Session either.
    """
    subjects = FakeSubjects(erased_at=datetime(2026, 8, 1, tzinfo=UTC))
    conversations = FakeConversations()
    runs = FakeRuns()

    with pytest.raises(ErasedSubjectRefused):
        await _ingestion(subjects, conversations, runs).run_for(
            binding=BINDING, event=EVENT, request_id="req-1"
        )

    assert runs.created == []
    assert runs.submitted == []


async def test_a_first_message_opens_one_persistent_conversation() -> None:
    """Persistent because a chat is a thread. An ephemeral Session would
    throw away the exchange between two messages that are obviously one
    conversation to the person typing them."""
    subjects = FakeSubjects()
    conversations = FakeConversations(known=None)
    runs = FakeRuns()

    await _ingestion(subjects, conversations, runs).run_for(
        binding=BINDING, event=EVENT, request_id="req-1"
    )

    assert subjects.calls == [(BINDING.workspace_id, "feishu", "ou_zhang")]
    assert runs.created == [(subjects.end_user_id, BINDING.agent_id, SessionMode.PERSISTENT)]
    assert conversations.remembered == [(BINDING.id, "ou_zhang", runs.session_id)]


async def test_a_second_message_continues_the_same_conversation() -> None:
    """The point of remembering it. Without this every message would start
    over, and the completion notifications §19.2 promises would arrive in a
    thread the person had never seen."""
    existing = uuid4()
    subjects = FakeSubjects()
    conversations = FakeConversations(known=existing)
    runs = FakeRuns()

    await _ingestion(subjects, conversations, runs).run_for(
        binding=BINDING, event=EVENT, request_id="req-2"
    )

    assert runs.created == []
    assert runs.submitted == [(subjects.end_user_id, existing, "om_1")]


async def test_the_event_id_is_the_idempotency_key() -> None:
    """§569's rule, and a second line rather than the first: §574's claim in
    `channel_events` is what stops a duplicate. This catches the narrower
    case where the claim was taken and the submission was retried after a
    crash between the two."""
    subjects = FakeSubjects()
    runs = FakeRuns()

    await _ingestion(subjects, FakeConversations(known=uuid4()), runs).run_for(
        binding=BINDING, event=EVENT, request_id="req-3"
    )

    assert runs.submitted[0][2] == "om_1"


async def test_a_blocked_session_answers_with_the_notice_attached_to_the_run() -> None:
    """§497 on the surface that needs it most. The pending Run is saved —
    that is allowed — but the transport gets the reason with it, so it
    cannot deliver silence to somebody who will read silence as a lost
    message and send it again."""
    blocking = uuid4()
    runs = FakeRuns(
        document={
            "queue": {
                "status": "session_blocked",
                "position": 1,
                "blocked_by_run_id": str(blocking),
                "head_status": "waiting_approval",
                "head_reason": {"pause_reason": None, "wait_kind": "user_confirmation"},
                "available_actions": ["approve", "reject"],
            }
        }
    )

    delivered = await _ingestion(FakeSubjects(), FakeConversations(known=uuid4()), runs).run_for(
        binding=BINDING, event=EVENT, request_id="req-4"
    )

    assert delivered.blocked is not None
    assert delivered.blocked.blocked_by_run_id == blocking
    assert delivered.blocked.available_actions == ("approve", "reject")
    # The Run really was saved — §497 permits the queue, it forbids silence.
    assert len(runs.submitted) == 1


async def test_an_unblocked_delivery_carries_no_notice() -> None:
    runs = FakeRuns(document={"queue": {"status": "queued", "position": 0}})

    delivered = await _ingestion(FakeSubjects(), FakeConversations(known=uuid4()), runs).run_for(
        binding=BINDING, event=EVENT, request_id="req-5"
    )

    assert delivered.blocked is None
