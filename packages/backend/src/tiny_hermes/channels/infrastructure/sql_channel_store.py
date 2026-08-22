"""Claiming an inbound delivery, exactly once.

The claim is the whole point of this module. Feishu states delivery is
at-least-once and retries on a schedule (M1's laboratory note recorded the
schedule: 15s, 5min, 1h, 6h), so the same `channel_event_id` arrives more
than once and can arrive twice in the same instant.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.channels.application.ingestion import ChannelBindingRecord
from tiny_hermes.channels.infrastructure.tables import (
    ChannelBindingRow,
    ChannelConversationRow,
    ChannelEventRow,
)


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

    async def attach_run(self, event_row_id: UUID, run_id: UUID) -> None:
        row = await self._session.get(ChannelEventRow, event_row_id)
        if row is not None:
            row.run_id = run_id
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
