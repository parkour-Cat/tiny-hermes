"""A claimed delivery becomes a Run, as the person who sent it.

§122: a Feishu user does not become a workspace member. They are an
`EndUser`, and the path from here is the one the end-user entry already
built — `external_identities` for the mapping (§282), `caller_type=end_user`
for the Session, and the subject's own private memory. Feishu is a new
*transport* onto that path, not a second identity system.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from tiny_hermes.channels.domain.events import ChannelEvent
from tiny_hermes.runs.domain.models import SessionMode, SessionSnapshot
from tiny_hermes.runs.ports.store import AcceptedRun


class ErasedSubjectRefused(Exception):
    """§344's erasure, holding across transports.

    The web path refuses an erased subject at credential exchange
    (`EndUserIdentityService.exchange`). That check lives in the service, not
    in `upsert_external_identity`, so a second transport that forgot it
    would be a way *around* an erasure the first one honours — the subject
    would be resurrected by walking in through a different door.
    """


@dataclass(frozen=True)
class ChannelBindingRecord:
    id: UUID
    workspace_id: UUID
    agent_id: UUID
    channel: str


@dataclass(frozen=True)
class UpsertedSubject:
    end_user_id: UUID
    erased_at: datetime | None


class SubjectDirectory(Protocol):
    async def upsert_external_identity(
        self, workspace_id: UUID, channel: str, external_user_id: str
    ) -> UpsertedSubject: ...


class Conversations(Protocol):
    async def session_for(
        self, binding_id: UUID, external_user_id: str
    ) -> UUID | None: ...

    async def remember_session(
        self, binding_id: UUID, external_user_id: str, session_id: UUID
    ) -> None: ...


class RunEntry(Protocol):
    """The two calls the web entry already makes. Same methods, same
    arguments — if this needed its own variants, Feishu would be a second
    execution path rather than a second transport."""

    async def create_end_user_session(
        self,
        workspace_id: UUID,
        end_user_id: UUID,
        agent_id: UUID,
        session_mode: SessionMode,
        request_id: str,
    ) -> SessionSnapshot: ...

    async def submit_end_user_run(
        self,
        workspace_id: UUID,
        end_user_id: UUID,
        session_id: UUID,
        text: str,
        idempotency_key: str | None,
        request_id: str,
    ) -> AcceptedRun: ...


@dataclass(frozen=True)
class ChannelIngestion:
    subjects: SubjectDirectory
    conversations: Conversations
    runs: RunEntry

    async def run_for(
        self,
        *,
        binding: ChannelBindingRecord,
        event: ChannelEvent,
        request_id: str,
    ) -> AcceptedRun:
        """The claimed delivery, turned into work.

        The idempotency key is the `channel_event_id` (§569's own rule: the
        key belongs to the caller's own notion of a request). It is a second
        line rather than the first — §574's claim in `channel_events` is what
        actually stops a duplicate, and this catches the narrower case where
        a claim was taken and the Run submission was retried after a crash
        between the two.
        """
        subject = await self.subjects.upsert_external_identity(
            binding.workspace_id, binding.channel, event.external_user_id
        )
        if subject.erased_at is not None:
            raise ErasedSubjectRefused

        session_id = await self.conversations.session_for(
            binding.id, event.external_user_id
        )
        if session_id is None:
            created = await self.runs.create_end_user_session(
                binding.workspace_id,
                subject.end_user_id,
                binding.agent_id,
                # Persistent, not ephemeral: a chat is a thread somebody
                # comes back to, and an ephemeral Session would discard the
                # conversation between two messages that are, to the person
                # typing them, obviously one exchange.
                SessionMode.PERSISTENT,
                request_id,
            )
            session_id = created.id
            await self.conversations.remember_session(
                binding.id, event.external_user_id, session_id
            )

        return await self.runs.submit_end_user_run(
            binding.workspace_id,
            subject.end_user_id,
            session_id,
            event.text,
            event.channel_event_id,
            request_id,
        )


__all__ = [
    "ChannelBindingRecord",
    "ChannelIngestion",
    "Conversations",
    "ErasedSubjectRefused",
    "RunEntry",
    "SubjectDirectory",
    "UpsertedSubject",
]
