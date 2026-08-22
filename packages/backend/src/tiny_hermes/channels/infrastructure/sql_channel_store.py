"""Claiming an inbound delivery, exactly once.

The claim is the whole point of this module. Feishu states delivery is
at-least-once and retries on a schedule (M1's laboratory note recorded the
schedule: 15s, 5min, 1h, 6h), so the same `channel_event_id` arrives more
than once and can arrive twice in the same instant.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.channels.infrastructure.tables import ChannelEventRow


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
