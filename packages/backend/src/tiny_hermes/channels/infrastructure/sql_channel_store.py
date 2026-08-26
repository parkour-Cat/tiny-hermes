"""Claiming an inbound delivery, exactly once.

The claim is the whole point of this module. Feishu states delivery is
at-least-once and retries on a schedule (M1's laboratory note recorded the
schedule: 15s, 5min, 1h, 6h), so the same `channel_event_id` arrives more
than once and can arrive twice in the same instant.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import delete, select, true, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.channels.application.ingestion import ChannelBindingRecord
from tiny_hermes.channels.domain.blocked import BlockedNotice, notice_from_stored
from tiny_hermes.channels.infrastructure.tables import (
    ChannelBindingRow,
    ChannelConversationRow,
    ChannelEventRow,
)
from tiny_hermes.runs.domain.models import (
    TERMINAL_STATES,
    RunState,
    message_from_document,
)
from tiny_hermes.runs.infrastructure.tables import RunRow, SessionMessageRow

#: The states in which "还在处理" is a true sentence. A whitelist rather than
#: "not terminal", because the difference is not cosmetic: a `paused` or
#: `waiting_approval` Run is **not** working — it is stopped, waiting for a
#: person — and telling its sender it is still being worked on is a
#: statement the platform knows to be false. `queued` is included because
#: from the sender's side waiting for a worker is indistinguishable from
#: being worked on, and both mean "it is coming".
_WORKING = (RunState.QUEUED.value, RunState.RUNNING.value)


@dataclass(frozen=True)
class PendingCard:
    """A delivery that has work under way and nothing on screen yet."""

    event_row_id: UUID
    run_id: UUID
    session_id: UUID
    #: Present when this message landed in a queue. It opens with the queue
    #: card directly — 「正在处理」 would be false, nothing is processing it.
    notice: BlockedNotice | None


@dataclass(frozen=True)
class PendingProgress:
    """A Run still going, whose sender has been told nothing yet."""

    event_row_id: UUID
    run_id: UUID
    session_id: UUID


@dataclass(frozen=True)
class PendingRefusal:
    """A delivery this build could not read, and the person owed the news."""

    event_row_id: UUID
    binding_id: UUID
    kind: str
    external_user_id: str


@dataclass(frozen=True)
class PendingNotice:
    """A delivery that landed behind a blocked head and has not been told."""

    event_row_id: UUID
    run_id: UUID
    session_id: UUID
    notice: BlockedNotice


@dataclass(frozen=True)
class PendingReply:
    """A finished Run whose channel sender has not been answered yet."""

    event_row_id: UUID
    run_id: UUID
    session_id: UUID
    state: RunState
    #: The Agent's last words, already flattened out of the stored message
    #: document. Empty is a real answer — a Run can complete having said
    #: nothing — and the domain decides what to tell a person about that.
    said: str
    failure_reason: str | None
    attempts: int


def _text_of(content: Any) -> str:
    """The words of a stored assistant turn, through the one parser there is.

    `CanonicalMessage.text` already drops tool calls and results and joins
    what is left; re-deriving that here would be a second answer to "what
    did the Agent say", and the two would part company the first time a
    block type was added.

    `None` when the lateral found no assistant turn — a Run can complete
    having written nothing, and the domain decides what to say about that.
    """
    if not isinstance(content, dict):
        return ""
    return message_from_document({"role": "assistant", **cast(dict[str, Any], content)}).text


def _failure_in(checkpoint: dict[str, Any] | None) -> str | None:
    """Why the Run failed, out of the checkpoint the Worker writes.

    Deliberately the same key `runs/infrastructure/sql_store._failure_reason`
    reads. Two readers of one field is already one too many; a channel that
    invented its own would drift the first time the Worker changed shape.
    """
    if not checkpoint:
        return None
    value: Any = checkpoint.get("failure")
    return str(value) if isinstance(value, str) and value else None


@dataclass(frozen=True)
class DeliveryTarget:
    """Everything the outbound consumer needs to reply to one participant."""

    binding_id: UUID
    workspace_id: UUID
    channel: str
    external_user_id: str
    app_id: str | None
    #: `None` for a receive-only binding — the consumer sends nothing.
    app_secret_ref: str | None
    #: A disabled binding still maps a session, but its channel is closed;
    #: the consumer must not reply through a door an administrator shut.
    binding_active: bool


class SqlChannelStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_delivery(
        self, binding_id: UUID, channel_event_id: str, now: datetime
    ) -> UUID | None:
        """The claim, in one statement. `None` means somebody else has it.

        A `SELECT` followed by an `INSERT` cannot do this: two concurrent
        retries of the same event both find nothing, both insert, and the
        channel gets two Runs for one message. That is not a narrow race —
        it is the ordinary case, because the retries *are* concurrent.

        `ON CONFLICT DO NOTHING ... RETURNING` collapses the question into
        the write itself: the unique index decides who was first, and the
        loser gets an empty result rather than an error to interpret. The
        caller creates a Run only when it holds the claim, so the claim is
        taken **before** the Run exists — a Run created first and claimed
        afterwards would be the duplicate this table exists to prevent.
        """
        claimed = await self._session.execute(
            insert(ChannelEventRow)
            .values(
                id=uuid4(),
                channel_binding_id=binding_id,
                channel_event_id=channel_event_id,
                received_at=now,
                run_id=None,
            )
            .on_conflict_do_nothing(constraint="uq_channel_events_delivery")
            .returning(ChannelEventRow.id)
        )
        return claimed.scalar_one_or_none()

    async def active_binding(self, binding_id: UUID) -> ChannelBindingRecord | None:
        """`None` for unknown *and* for disabled, deliberately.

        A disabled binding that answered differently from an unknown one
        would let anyone with the URL learn which bindings exist, and the
        answer to both is the same refusal.
        """
        row = await self._session.scalar(
            select(ChannelBindingRow).where(
                ChannelBindingRow.id == binding_id,
                ChannelBindingRow.status == "active",
            )
        )
        if row is None:
            return None
        return ChannelBindingRecord(
            id=row.id,
            workspace_id=row.workspace_id,
            agent_id=row.agent_id,
            channel=row.channel,
        )

    async def encrypt_key_ref_of(self, binding_id: UUID) -> str | None:
        return await self._session.scalar(
            select(ChannelBindingRow.encrypt_key_ref).where(
                ChannelBindingRow.id == binding_id
            )
        )

    async def session_for(self, binding_id: UUID, external_user_id: str) -> UUID | None:
        return await self._session.scalar(
            select(ChannelConversationRow.session_id).where(
                ChannelConversationRow.channel_binding_id == binding_id,
                ChannelConversationRow.external_user_id == external_user_id,
            )
        )

    async def delivery_target_for(
        self, session_id: UUID
    ) -> "DeliveryTarget | None":
        """Where a finished Run's result goes, or `None` if it goes nowhere.

        The reverse of `session_for`: a Run carries a `session_id`, and the
        outbound consumer needs to know which channel — and which participant
        on it — that session belongs to. `None` is the common case and not an
        error: an ordinary console Run has no channel conversation, and its
        result is read on the web, not pushed to Feishu.

        Returns the reply credentials alongside the recipient so the consumer
        makes one query rather than three. `app_secret_ref` may be `None` —
        a receive-only binding — and the consumer treats that as "nowhere to
        reply", which is the whole reason it is nullable.
        """
        row = (
            await self._session.execute(
                select(
                    ChannelConversationRow.channel_binding_id,
                    ChannelConversationRow.external_user_id,
                    ChannelBindingRow.workspace_id,
                    ChannelBindingRow.channel,
                    ChannelBindingRow.app_id,
                    ChannelBindingRow.app_secret_ref,
                    ChannelBindingRow.status,
                )
                .join(
                    ChannelBindingRow,
                    ChannelBindingRow.id == ChannelConversationRow.channel_binding_id,
                )
                .where(ChannelConversationRow.session_id == session_id)
            )
        ).first()
        if row is None:
            return None
        return DeliveryTarget(
            binding_id=row.channel_binding_id,
            workspace_id=row.workspace_id,
            channel=row.channel,
            external_user_id=row.external_user_id,
            app_id=row.app_id,
            app_secret_ref=row.app_secret_ref,
            binding_active=row.status == "active",
        )

    async def remember_session(
        self, binding_id: UUID, external_user_id: str, session_id: UUID
    ) -> None:
        """`ON CONFLICT DO NOTHING` for the same reason `claim_delivery` uses
        it: two first messages from one person can be in flight at once, and
        the loser must keep the winner's thread rather than raise."""
        await self._session.execute(
            insert(ChannelConversationRow)
            .values(
                id=uuid4(),
                channel_binding_id=binding_id,
                external_user_id=external_user_id,
                session_id=session_id,
            )
            .on_conflict_do_nothing(constraint="uq_channel_conversations_participant")
        )
        await self._session.flush()

    async def record_blocked_notice(
        self, event_row_id: UUID, notice: BlockedNotice
    ) -> None:
        """§497's facts, kept as they were when this message landed.

        Written in the inbound transaction beside `attach_run`, so the
        notice and the Run it describes commit together. Re-deriving it when
        the scan gets there would read a queue that has usually already
        moved — and in the common case where the head unblocks quickly, that
        means saying nothing at all to somebody who really was made to wait.
        """
        await self._session.execute(
            update(ChannelEventRow)
            .where(ChannelEventRow.id == event_row_id)
            .values(blocked_notice=notice.document())
        )
        await self._session.flush()

    async def pending_card_opens(self, limit: int = 50) -> list["PendingCard"]:
        """Deliveries that have a Run and no card yet.

        No age condition: this is the one stage that must not wait. It runs
        on the very next scan after the delivery lands, which is what makes
        the person see something about a second after they hit send.
        """
        rows = (
            await self._session.execute(
                select(
                    ChannelEventRow.id,
                    ChannelEventRow.run_id,
                    ChannelEventRow.blocked_notice,
                    RunRow.session_id,
                )
                .join(RunRow, RunRow.id == ChannelEventRow.run_id)
                .where(
                    ChannelEventRow.run_id.is_not(None),
                    ChannelEventRow.card_message_id.is_(None),
                    ChannelEventRow.replied_at.is_(None),
                    ChannelEventRow.card_attempted_at.is_(None),
                )
                .order_by(ChannelEventRow.received_at, ChannelEventRow.id)
                .limit(limit)
            )
        ).all()
        return [
            PendingCard(
                event_row_id=row.id,
                run_id=row.run_id,
                session_id=row.session_id,
                notice=notice_from_stored(row.blocked_notice),
            )
            for row in rows
        ]

    async def record_card(
        self, event_row_id: UUID, message_id: str | None, now: datetime
    ) -> None:
        """Remember the card, or remember that there is none.

        `card_attempted_at` is stamped either way, and that is the whole
        reason it exists separately from `card_message_id`. A send that
        answered without an id would otherwise stay in the opening scan
        forever, sending a new 「正在处理」 card every second.
        """
        await self._session.execute(
            update(ChannelEventRow)
            .where(ChannelEventRow.id == event_row_id)
            .values(card_message_id=message_id, card_attempted_at=now)
        )
        await self._session.flush()

    async def card_for(self, event_row_id: UUID) -> str | None:
        return await self._session.scalar(
            select(ChannelEventRow.card_message_id).where(
                ChannelEventRow.id == event_row_id
            )
        )

    async def record_unsupported(
        self, event_row_id: UUID, kind: str, external_user_id: str
    ) -> None:
        """Mark a claimed delivery as one this build could not read, and who
        sent it. Both, because the refusal needs to say what and reach whom."""
        await self._session.execute(
            update(ChannelEventRow)
            .where(ChannelEventRow.id == event_row_id)
            .values(
                unsupported_kind=kind[:64],
                unsupported_open_id=external_user_id[:200],
            )
        )
        await self._session.flush()

    async def pending_progress_notices(
        self, older_than: datetime, limit: int = 50
    ) -> list["PendingProgress"]:
        """Deliveries whose Run is still going and whose sender has not been
        told so.

        Two separate exclusions, and the tests needed both. `blocked_notice
        IS NULL` drops the message that landed *behind* a blocked head — the
        card already told that person, with the reason. `_WORKING` drops the
        blocking head **itself**, which is not terminal and so passed a
        "not finished" predicate while being paused rather than working.
        The first version used that predicate and told the person whose Run
        somebody had paused that it was still being worked on.

        The Run's state is read here rather than trusted from the row,
        because it is the one fact that changes while the row sits there.
        """
        rows = (
            await self._session.execute(
                select(
                    ChannelEventRow.id,
                    ChannelEventRow.run_id,
                    RunRow.session_id,
                )
                .join(RunRow, RunRow.id == ChannelEventRow.run_id)
                .where(
                    ChannelEventRow.run_id.is_not(None),
                    ChannelEventRow.replied_at.is_(None),
                    ChannelEventRow.progress_notified_at.is_(None),
                    ChannelEventRow.blocked_notice.is_(None),
                    ChannelEventRow.received_at < older_than,
                    RunRow.status.in_(_WORKING),
                )
                .order_by(ChannelEventRow.received_at, ChannelEventRow.id)
                .limit(limit)
            )
        ).all()
        return [
            PendingProgress(
                event_row_id=row.id, run_id=row.run_id, session_id=row.session_id
            )
            for row in rows
        ]

    async def settle_progress_notice(
        self, event_row_id: UUID, now: datetime
    ) -> None:
        """Its own stamp, never `replied_at` — the answer still has to go."""
        await self._session.execute(
            update(ChannelEventRow)
            .where(ChannelEventRow.id == event_row_id)
            .values(progress_notified_at=now)
        )
        await self._session.flush()

    async def pending_refusals(self, limit: int = 50) -> list["PendingRefusal"]:
        """Unreadable deliveries still owed an answer.

        No join to `runs` — an unreadable message starts none. That is why
        this is its own scan rather than a branch inside `pending_replies`:
        the reply scan's whole predicate is "the Run finished", and there is
        no Run here to have finished.

        The recipient comes from `channel_conversations` when the sender has
        talked to this Agent before, and from the delivery itself when they
        have not. A first-ever message that happens to be a photo is exactly
        the case with no conversation row, and it is the one most in need of
        an answer.
        """
        rows = (
            await self._session.execute(
                select(
                    ChannelEventRow.id,
                    ChannelEventRow.channel_binding_id,
                    ChannelEventRow.unsupported_kind,
                    ChannelEventRow.unsupported_open_id,
                )
                .where(
                    ChannelEventRow.unsupported_kind.is_not(None),
                    ChannelEventRow.replied_at.is_(None),
                )
                .order_by(ChannelEventRow.received_at, ChannelEventRow.id)
                .limit(limit)
            )
        ).all()
        return [
            PendingRefusal(
                event_row_id=row.id,
                binding_id=row.channel_binding_id,
                kind=row.unsupported_kind,
                external_user_id=row.unsupported_open_id,
            )
            for row in rows
            if row.unsupported_open_id
        ]

    async def binding_target(self, binding_id: UUID) -> "DeliveryTarget | None":
        """The reply credentials for a binding, with no conversation needed.

        `delivery_target_for` starts from a session; a refusal has none. The
        columns are the same, so the same type comes back — with the
        recipient left to the caller, who got it from the delivery.
        """
        row = (
            await self._session.execute(
                select(
                    ChannelBindingRow.id,
                    ChannelBindingRow.workspace_id,
                    ChannelBindingRow.channel,
                    ChannelBindingRow.app_id,
                    ChannelBindingRow.app_secret_ref,
                    ChannelBindingRow.status,
                ).where(ChannelBindingRow.id == binding_id)
            )
        ).first()
        if row is None:
            return None
        return DeliveryTarget(
            binding_id=row.id,
            workspace_id=row.workspace_id,
            channel=row.channel,
            external_user_id="",
            app_id=row.app_id,
            app_secret_ref=row.app_secret_ref,
            binding_active=row.status == "active",
        )

    async def pending_blocked_notices(
        self, limit: int = 50
    ) -> list["PendingNotice"]:
        """Deliveries that landed in a queue and have not been told so.

        Oldest first with `id` breaking the tie, like `pending_replies` and
        for the same reason: `received_at` is not a total order.
        """
        rows = (
            await self._session.execute(
                select(
                    ChannelEventRow.id,
                    ChannelEventRow.run_id,
                    ChannelEventRow.blocked_notice,
                    RunRow.session_id,
                )
                .join(RunRow, RunRow.id == ChannelEventRow.run_id)
                .where(
                    ChannelEventRow.blocked_notice.is_not(None),
                    ChannelEventRow.blocked_notified_at.is_(None),
                )
                .order_by(ChannelEventRow.received_at, ChannelEventRow.id)
                .limit(limit)
            )
        ).all()
        found: list[PendingNotice] = []
        for row in rows:
            notice = notice_from_stored(row.blocked_notice)
            if notice is None:
                continue
            found.append(
                PendingNotice(
                    event_row_id=row.id,
                    run_id=row.run_id,
                    session_id=row.session_id,
                    notice=notice,
                )
            )
        return found

    async def settle_blocked_notice(
        self, event_row_id: UUID, now: datetime
    ) -> None:
        """Its own stamp, never `replied_at`.

        Settling the reply here would end the delivery before the answer
        exists — the person would be told they are queued and then never
        hear the result, which is the silence this whole path exists to end.
        """
        await self._session.execute(
            update(ChannelEventRow)
            .where(ChannelEventRow.id == event_row_id)
            .values(blocked_notified_at=now)
        )
        await self._session.flush()

    async def attach_run(self, event_row_id: UUID, run_id: UUID) -> None:
        """Records which Run this delivery produced.

        Had no caller for a whole milestone: a live deployment's
        `channel_events` held two rows with `run_id` NULL while both Runs
        existed and had completed. Nothing noticed because nothing read the
        column. It is the outbound queue's key now, so a regression here
        stops replies rather than quietly losing an audit link.
        """
        row = await self._session.get(ChannelEventRow, event_row_id)
        if row is not None:
            row.run_id = run_id
        await self._session.flush()

    async def pending_replies(self, limit: int = 50) -> list[PendingReply]:
        """Deliveries whose Run has finished and whose sender is still waiting.

        Ordered oldest-first so a backlog drains in the order people sent
        their messages. `received_at` alone is not a total order — two
        deliveries can share a timestamp — so `id` breaks the tie, per this
        repository's rule about ordering without one.

        The Agent's last turn is read here, in the same query, by a lateral
        over `session_messages`. Fetching it per row afterwards would be a
        second round trip for every reply and would let the two reads see
        different states of the same Run.

        The stored document comes back whole and is flattened by
        `message_from_document` rather than by a `jsonb_path_query` — the
        first version did the latter, and it was a second place that decided
        what the text of a message is. Two such places drift the moment a
        block type is added, and this one would drift silently: an
        unrecognised block would come back as an empty reply, not an error.
        """
        said = (
            select(SessionMessageRow.content.label("content"))
            .where(
                SessionMessageRow.source_run_id == RunRow.id,
                SessionMessageRow.role == "assistant",
                # Not "prevents a leak" — this reply goes to the same person
                # who withdrew the message, so nothing leaks. It is that the
                # channel and the model's own context must tell one story: a
                # sender who received an answer no longer in the model's
                # history gets no coherent response when they follow up on
                # it. The `outerjoin` below already handles no row matching
                # here (a Run can finish having said nothing), so a withdrawn
                # newest turn just falls back to the next assistant turn, or
                # to that same empty-reply path if there isn't one.
                SessionMessageRow.withdrawn_at.is_(None),
            )
            .order_by(SessionMessageRow.sequence.desc())
            .limit(1)
            .lateral("said")
        )
        rows = (
            await self._session.execute(
                select(
                    ChannelEventRow.id,
                    ChannelEventRow.run_id,
                    ChannelEventRow.reply_attempts,
                    RunRow.session_id,
                    RunRow.status,
                    RunRow.checkpoint,
                    said.c.content,
                )
                .join(RunRow, RunRow.id == ChannelEventRow.run_id)
                .outerjoin(said, true())
                .where(
                    ChannelEventRow.run_id.is_not(None),
                    ChannelEventRow.replied_at.is_(None),
                    RunRow.status.in_([state.value for state in TERMINAL_STATES]),
                )
                .order_by(ChannelEventRow.received_at, ChannelEventRow.id)
                .limit(limit)
            )
        ).all()
        return [
            PendingReply(
                event_row_id=row.id,
                run_id=row.run_id,
                session_id=row.session_id,
                state=RunState(row.status),
                said=_text_of(row.content),
                failure_reason=_failure_in(row.checkpoint),
                attempts=row.reply_attempts,
            )
            for row in rows
        ]

    async def settle_reply(self, event_row_id: UUID, note: str, now: datetime) -> None:
        """The delivery is done being owed an answer, however it ended.

        One method for "sent" and for "deliberately not sent", because the
        queue has to drain either way. `note` is the difference, and it is
        the thing an operator reads when somebody says no reply arrived.
        """
        await self._session.execute(
            update(ChannelEventRow)
            .where(ChannelEventRow.id == event_row_id)
            .values(replied_at=now, reply_note=note[:200])
        )
        await self._session.flush()

    async def record_reply_attempt(self, event_row_id: UUID) -> None:
        """One try about to be spent, counted **before** it is spent.

        Counted first because a send that hangs or kills the process would
        otherwise cost nothing, and a target that reliably crashes the
        dispatcher would be retried forever — the bound would hold only for
        failures polite enough to raise.

        Incremented in SQL rather than read-modify-written: two dispatcher
        replicas scanning at once would both read the same count and both
        write the same successor, and the bound would quietly stop being one.
        """
        await self._session.execute(
            update(ChannelEventRow)
            .where(ChannelEventRow.id == event_row_id)
            .values(reply_attempts=ChannelEventRow.reply_attempts + 1)
        )
        await self._session.flush()

    async def note_reply_failure(self, event_row_id: UUID, note: str) -> None:
        """Why the last try failed, with no stamp — this row comes back."""
        await self._session.execute(
            update(ChannelEventRow)
            .where(ChannelEventRow.id == event_row_id)
            .values(reply_note=note[:200])
        )
        await self._session.flush()

    async def forget_deliveries_before(self, cutoff: datetime) -> int:
        """§574's seven days, swept.

        `audit_events` shipped append-only with no cleanup and is recorded
        as a debt in its own verification note; this table is not going to
        repeat that. Deleting a claim after the retention window is safe
        because Feishu's last retry is six hours out — a claim old enough to
        be swept can no longer be contested.
        """
        # `RETURNING id` rather than `rowcount`: the count is the point of
        # this method's return value, and a `Result`'s `rowcount` is not
        # typed as available on every dialect. Counting the rows the delete
        # actually names is both checkable and dialect-independent.
        removed = await self._session.scalars(
            delete(ChannelEventRow)
            .where(ChannelEventRow.received_at < cutoff)
            .returning(ChannelEventRow.id)
        )
        swept = len(list(removed.all()))
        await self._session.flush()
        return swept
