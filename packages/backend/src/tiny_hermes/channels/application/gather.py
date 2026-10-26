"""What one person sent together, started as one Run.

Feishu sends a file and the words typed beside it as two messages, and does
not order them: in the real chat one file arrived 0.6 s before its question
and another question 0.25 s before its file. A Run started on arrival had the
words and not the file, or the file and not the words. So a message is held
on arrival (`ChannelIngestion.accept`) and this gathers, per person, whatever
arrived with no newer message for `window_seconds`, into one Run carrying all
of it (office-tasks design, 2026-10-26).

In the Scheduler, as its own scan rather than inside the reply scan: that
one is off in a deployment with no egress route, and such a deployment must
still receive messages and run them.
"""

import logging
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from tiny_hermes.channels.application.ingestion import (
    ChannelBindingRecord,
    ChannelIngestion,
    ErasedSubjectRefused,
)
from tiny_hermes.channels.domain.blocked import BlockedNotice
from tiny_hermes.channels.domain.events import ChannelEvent
from tiny_hermes.channels.infrastructure.sql_channel_store import HeldMessage

logger = logging.getLogger(__name__)

#: How long one person's messages are held for a later one to join them.
#: Measured, not guessed: the two halves of one send arrived 0.25 s and 0.6 s
#: apart in the real chat. Every message pays this as latency, so it is kept
#: no longer than it needs to be.
DEFAULT_GATHER_SECONDS = 1.5


class GatherNote:
    """What `reply_note` records for rows that will get no answer of their own."""

    #: Went into a batch whose last message carries the Run and its answer.
    GATHERED = "gathered"
    ERASED = "erased"
    BINDING_DISABLED = "binding_disabled"
    #: The batch, put together, read as a command. Commands are carried out
    #: on arrival, so this is not expected; noted rather than guessed at.
    NOT_A_RUN = "gathered_not_a_run"


class GatherStore(Protocol):
    async def senders_ready(
        self, older_than: datetime, limit: int = 50
    ) -> list[tuple[UUID, str]]: ...

    async def take_batch(
        self, binding_id: UUID, external_user_id: str
    ) -> list[HeldMessage]: ...

    async def mark_taken(self, event_row_ids: list[UUID]) -> None: ...

    async def active_binding(self, binding_id: UUID) -> ChannelBindingRecord | None: ...

    async def attach_run(self, event_row_id: UUID, run_id: UUID) -> None: ...

    async def record_blocked_notice(
        self, event_row_id: UUID, notice: BlockedNotice
    ) -> None: ...

    async def settle_reply(self, event_row_id: UUID, note: str, now: datetime) -> None: ...

    def savepoint(self) -> AbstractAsyncContextManager[object]: ...


@dataclass(frozen=True)
class ChannelGatherer:
    store: GatherStore
    ingestion: ChannelIngestion
    window_seconds: float = DEFAULT_GATHER_SECONDS
    batch_size: int = 50

    async def gather_once(self) -> int:
        """One pass. Returns how many Runs it started.

        Each person in a savepoint of their own: a batch that cannot be
        started — a bug, a row this build cannot read — must not hold up
        everybody else's messages behind it every second from now on.
        """
        older_than = _now() - timedelta(seconds=self.window_seconds)
        started = 0
        for binding_id, sender in await self.store.senders_ready(older_than, self.batch_size):
            try:
                async with self.store.savepoint():
                    if await self._gather(binding_id, sender):
                        started += 1
            except Exception:
                logger.exception("channel gather failed: binding=%s", binding_id)
        return started

    async def _gather(self, binding_id: UUID, sender: str) -> bool:
        batch = await self.store.take_batch(binding_id, sender)
        if not batch or all(_files_only(held.event) for held in batch):
            # Files with no words yet: they wait, and the question scan asks
            # what they are for. Also nothing at all, when another Scheduler
            # holds the rows.
            return False
        rows = [held.event_row_id for held in batch]
        await self.store.mark_taken(rows)

        binding = await self.store.active_binding(binding_id)
        if binding is None:
            await self._settle(rows, GatherNote.BINDING_DISABLED)
            return False
        try:
            delivered = await self.ingestion.run_for(
                binding=binding,
                event=_together(batch),
                request_id=f"gather_{uuid4().hex}",
            )
        except ErasedSubjectRefused:
            await self._settle(rows, GatherNote.ERASED)
            return False
        if delivered.run is None:
            logger.warning("channel gather produced no run: binding=%s", binding_id)
            await self._settle(rows, GatherNote.NOT_A_RUN)
            return False

        # The last message carries the Run, so the card, the progress note
        # and the answer go out once, under the newest message's row; the
        # rest are settled as part of it.
        carrier = batch[-1].event_row_id
        await self.store.attach_run(carrier, delivered.run.run_id)
        await self._settle(rows[:-1], GatherNote.GATHERED)
        if delivered.blocked is not None:
            await self.store.record_blocked_notice(carrier, delivered.blocked)
        return True

    async def _settle(self, rows: list[UUID], note: str) -> None:
        now = _now()
        for row in rows:
            await self.store.settle_reply(row, note, now)


def _files_only(event: ChannelEvent) -> bool:
    return not event.text.strip() and not event.images


def _together(batch: list[HeldMessage]) -> ChannelEvent:
    """The batch as the one message the person meant.

    Words in the order sent, a blank line between, and every picture and
    file. Keyed on the last message, the one whose row carries the Run.
    """
    last = batch[-1].event
    return ChannelEvent(
        channel=last.channel,
        channel_event_id=last.channel_event_id,
        external_user_id=last.external_user_id,
        text="\n\n".join(
            words for held in batch if (words := held.event.text.strip())
        ),
        images=tuple(image for held in batch for image in held.event.images),
        files=tuple(attached for held in batch for attached in held.event.files),
    )


def _now() -> datetime:
    return datetime.now(UTC)
