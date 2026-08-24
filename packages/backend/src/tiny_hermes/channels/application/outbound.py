"""A finished Run, sent back to the person who started it from a channel.

The half §19.3 names fourth — "发送进度、结果和审批通知" — and the half that
did not exist while the other three did. A Feishu message became a Run, the
Run completed, the Agent wrote an answer, and the answer stayed in the
database: from the sender's side, indistinguishable from a platform that
had eaten their message. That is this repository's own recurring bug, and
it had a chat window in front of it.

Why a scan rather than a callback on the Worker. The Worker finishes Runs
in its own transaction and can die between committing the Run and telling
anybody; a callback there would lose the reply exactly when it matters, and
nothing would be left to say so. A row in `channel_events` survives, and a
scan that finds it again is a retry nobody has to write. The Scheduler
already owns this shape — bounded, idempotent, and holding an advisory
lock — so the dispatcher runs there.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from tiny_hermes.channels.domain.feishu import CHANNEL
from tiny_hermes.channels.domain.reply import reply_for
from tiny_hermes.channels.infrastructure.sql_channel_store import (
    DeliveryTarget,
    PendingReply,
)

logger = logging.getLogger(__name__)

#: How many times a refused send is tried before the row is settled. A
#: transient 5xx or a token that expired mid-flight clears well within this;
#: "bot is not in the chat" never does, and a row retried forever is a scan
#: that never drains.
DEFAULT_MAX_ATTEMPTS = 5


class ReplyOutcome:
    """What `reply_note` records. Values are read by operators and by tests,
    so they are stable strings rather than an enum that would tempt somebody
    to renumber them."""

    SENT = "sent"
    #: The Run finished but no channel conversation claims its session. Not
    #: expected — the row got here *through* a channel — so it is worth a
    #: distinct note rather than being folded into a generic skip.
    NO_TARGET = "no_target"
    #: A receive-only binding: no `app_secret_ref`, nothing to authenticate
    #: a send with. §929's drill binding is exactly this.
    NO_CREDENTIAL = "no_credential"
    BINDING_DISABLED = "binding_disabled"
    UNSUPPORTED_CHANNEL = "unsupported_channel"
    #: Prefix. The vendor's own refusal is appended, because "it was
    #: refused" and "it was refused because the bot is not in that chat" are
    #: different problems with different fixes.
    REFUSED = "refused"


@dataclass(frozen=True)
class _Recipient:
    """A delivery target that has everything a send needs."""

    app_id: str
    app_secret_ref: str
    open_id: str


class ChannelSender(Protocol):
    """Sending text to one participant on one channel.

    Feishu's is the only implementation today. The port is here rather than
    in the Feishu module so a second channel is a second sender and not a
    second dispatcher — the queue, the retry bound and the settling are not
    channel-specific and must not be copied.
    """

    async def send_text(
        self, *, app_id: str, app_secret: str, open_id: str, text: str
    ) -> None: ...


class ReplyQueue(Protocol):
    """Exactly what the dispatcher does to storage, and nothing else."""

    async def pending_replies(self, limit: int = 50) -> list[PendingReply]: ...

    async def delivery_target_for(self, session_id: UUID) -> DeliveryTarget | None: ...

    async def settle_reply(
        self, event_row_id: UUID, note: str, now: datetime
    ) -> None: ...

    async def record_reply_attempt(self, event_row_id: UUID) -> None: ...

    async def note_reply_failure(self, event_row_id: UUID, note: str) -> None: ...


class ChannelReplyDispatcher:
    def __init__(
        self,
        *,
        store: ReplyQueue,
        resolve_secret: Callable[[str], Awaitable[str]],
        sender: ChannelSender,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        batch_size: int = 50,
    ) -> None:
        self._store = store
        self._resolve_secret = resolve_secret
        self._sender = sender
        self._max_attempts = max_attempts
        self._batch_size = batch_size

    async def dispatch_once(self) -> int:
        """One bounded pass. Returns how many replies were actually sent.

        The count is sends, not rows touched: a pass that settled six rows
        because their bindings were disabled has replied to nobody, and a
        metric that called that six would say the channel was healthy.
        """
        sent = 0
        for pending in await self._store.pending_replies(self._batch_size):
            if await self._dispatch(pending):
                sent += 1
        return sent

    async def _dispatch(self, pending: PendingReply) -> bool:
        text = reply_for(
            state=pending.state,
            said=pending.said,
            failure_reason=pending.failure_reason,
        )
        if text is None:
            # The scan only selects terminal Runs, so this is unreachable
            # today. Left as a skip rather than an assertion because the
            # alternative — treating it as "nothing to say" and settling the
            # row — would silently drop a reply if the two ever disagreed.
            return False

        target = await self._store.delivery_target_for(pending.session_id)
        recipient = self._sendable(target)
        if isinstance(recipient, str):
            logger.info(
                "channel reply not sent: run=%s reason=%s", pending.run_id, recipient
            )
            await self._store.settle_reply(pending.event_row_id, recipient, _now())
            return False

        # Counted before the send, not after a failure: an attempt that
        # never comes back — a hang, a killed process — has still been
        # spent, and a bound that only counts polite failures is not a bound.
        await self._store.record_reply_attempt(pending.event_row_id)
        try:
            secret = await self._resolve_secret(recipient.app_secret_ref)
            await self._sender.send_text(
                app_id=recipient.app_id,
                app_secret=secret,
                open_id=recipient.open_id,
                text=text,
            )
        except Exception as error:
            await self._failed(pending, error)
            return False

        await self._store.settle_reply(
            pending.event_row_id, ReplyOutcome.SENT, _now()
        )
        return True

    def _sendable(self, target: DeliveryTarget | None) -> "_Recipient | str":
        """Somewhere to send this, or the note saying why not.

        Returns a narrowed recipient rather than validating in place: a
        `DeliveryTarget` carries `app_id` and `app_secret_ref` as optional
        because a receive-only binding is legitimate, and every caller past
        this point needs them present. Handing back a type that has them
        keeps that guarantee in the type system instead of in an assertion
        somebody can delete.

        Every rejection settles the row. A queue that only drained on
        success would keep re-examining a binding an administrator disabled
        a month ago, once an interval, until the seven-day sweep removed it.
        """
        if target is None:
            return ReplyOutcome.NO_TARGET
        if not target.binding_active:
            # The Run was in flight when somebody shut the channel. Disabling
            # is a decision about the channel, not about which Runs happened
            # to be running at the time.
            return ReplyOutcome.BINDING_DISABLED
        if target.channel != CHANNEL:
            return ReplyOutcome.UNSUPPORTED_CHANNEL
        if not target.app_secret_ref or not target.app_id:
            return ReplyOutcome.NO_CREDENTIAL
        return _Recipient(
            app_id=target.app_id,
            app_secret_ref=target.app_secret_ref,
            open_id=target.external_user_id,
        )

    async def _failed(self, pending: PendingReply, error: Exception) -> None:
        """A spent attempt, and the row either comes back or stops.

        `pending.attempts` is what the scan read, so the attempt just spent
        makes it `attempts + 1`. Comparing the stale value would give one
        try more than the bound says, which is the kind of off-by-one that
        only shows up as "it retried six times" in an incident.
        """
        note = f"{ReplyOutcome.REFUSED}:{error}"
        if pending.attempts + 1 >= self._max_attempts:
            logger.warning(
                "channel reply given up: run=%s attempts=%d last=%r",
                pending.run_id,
                pending.attempts + 1,
                error,
            )
            await self._store.settle_reply(pending.event_row_id, note, _now())
        else:
            await self._store.note_reply_failure(pending.event_row_id, note)
            logger.info(
                "channel reply failed, will retry: run=%s attempt=%d error=%r",
                pending.run_id,
                pending.attempts + 1,
                error,
            )


def _now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "ChannelReplyDispatcher",
    "ChannelSender",
    "ReplyOutcome",
    "ReplyQueue",
]
