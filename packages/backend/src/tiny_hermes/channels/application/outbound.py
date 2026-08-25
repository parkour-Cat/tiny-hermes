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
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from tiny_hermes.channels.domain.feishu import CHANNEL
from tiny_hermes.channels.domain.reply import (
    progress_note,
    refusal_for,
    reply_for,
)
from tiny_hermes.channels.infrastructure.feishu_card import blocked_card
from tiny_hermes.channels.infrastructure.sql_channel_store import (
    DeliveryTarget,
    PendingNotice,
    PendingProgress,
    PendingRefusal,
    PendingReply,
)

logger = logging.getLogger(__name__)

#: How many times a refused send is tried before the row is settled. A
#: transient 5xx or a token that expired mid-flight clears well within this;
#: "bot is not in the chat" never does, and a row retried forever is a scan
#: that never drains.
DEFAULT_MAX_ATTEMPTS = 5

#: Appended to a delivery's row id for the queue notice, so the notice and
#: the answer are two different keys to Feishu's deduplication.
_NOTICE_KEY_SUFFIX = ":q"

#: And for the progress note, so one delivery's three possible sends are
#: three distinct keys to Feishu's deduplication.
_PROGRESS_KEY_SUFFIX = ":p"

#: How long a Run may go before its sender is told it is still working.
#:
#: Was 20, chosen from a sample of ordinary Runs that measure about five
#: seconds. The live tenant then produced the case that mattered — ten files
#: created and words counted, **17.7 seconds** — and it missed the notice by
#: 2.3 seconds. The person who sent it described exactly the symptom this
#: notice exists to prevent: a long wait with nothing happening.
#:
#: 8 keeps the original reason intact — an ordinary five-second Run still
#: says nothing, because a notice on every message is noise and noise is
#: what stops people reading the messages that matter — while landing
#: inside the few seconds in which somebody starts wondering whether it
#: broke. Pinned by `test_a_run_of_a_dozen_seconds_is_slow_enough_to_mention`
#: rather than by this constant, so that raising it fails a test.
DEFAULT_PROGRESS_AFTER_SECONDS = 8


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
    #: A message this build could not read; the sender was told so.
    UNSUPPORTED_MESSAGE = "unsupported_message"
    #: Prefix. The vendor's own refusal is appended, because "it was
    #: refused" and "it was refused because the bot is not in that chat" are
    #: different problems with different fixes.
    REFUSED = "refused"


@dataclass(frozen=True)
class _Recipient:
    """A delivery target that has everything a send needs."""

    workspace_id: UUID
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
        self,
        *,
        app_id: str,
        app_secret: str,
        open_id: str,
        text: str,
        delivery_key: str | None = None,
    ) -> None: ...

    async def send_card(
        self,
        *,
        app_id: str,
        app_secret: str,
        open_id: str,
        card: dict[str, Any],
        delivery_key: str | None = None,
    ) -> None: ...


class ChannelSenders(Protocol):
    """A sender for one workspace's traffic.

    Per workspace rather than one shared sender, because §16.5's chain is
    platform ∩ workspace ∩ … and a request naming no workspace is measured
    against the platform alone. An installation that approved
    `open.feishu.cn` at the platform layer would then deliver replies for a
    workspace that never approved it — the workspace scope still in the
    database, still shown in the console, and meaning nothing.

    The Agent and Run layers are deliberately not named. A reply is the
    platform delivering its own notification; the Agent did not ask to call
    Feishu, and measuring it against an Agent's `network.allow` would
    require every Agent published to a channel to list the vendor.
    """

    def __call__(self, workspace_id: UUID, /) -> ChannelSender: ...


class ReplyQueue(Protocol):
    """Exactly what the dispatcher does to storage, and nothing else."""

    async def pending_replies(self, limit: int = 50) -> list[PendingReply]: ...

    async def pending_blocked_notices(
        self, limit: int = 50
    ) -> list[PendingNotice]: ...

    async def pending_refusals(self, limit: int = 50) -> list[PendingRefusal]: ...

    async def pending_progress_notices(
        self, older_than: datetime, limit: int = 50
    ) -> list[PendingProgress]: ...

    async def settle_progress_notice(
        self, event_row_id: UUID, now: datetime
    ) -> None: ...

    async def binding_target(self, binding_id: UUID) -> DeliveryTarget | None: ...

    async def settle_blocked_notice(
        self, event_row_id: UUID, now: datetime
    ) -> None: ...

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
        senders: ChannelSenders,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        batch_size: int = 50,
        console_url: str | None = None,
        progress_after_seconds: int = DEFAULT_PROGRESS_AFTER_SECONDS,
    ) -> None:
        self._store = store
        self._resolve_secret = resolve_secret
        self._senders = senders
        self._max_attempts = max_attempts
        self._batch_size = batch_size
        # `None` on a deployment that has not been told its own console
        # address. The card then offers no button rather than a guessed URL,
        # which would be a dead link in front of a user.
        self._console_url = console_url
        self._progress_after_seconds = progress_after_seconds

    async def dispatch_once(self) -> int:
        """One bounded pass. Returns how many messages were actually sent.

        The count is sends, not rows touched: a pass that settled six rows
        because their bindings were disabled has told nobody anything, and a
        metric that called that six would say the channel was healthy.

        Notices go before replies, deliberately. Both can be due in the same
        pass — a Run can queue and finish between two scans — and "your
        message is queued" arriving *after* the answer would be worse than
        not sending it at all.
        """
        sent = 0
        for unreadable in await self._store.pending_refusals(self._batch_size):
            if await self._refuse(unreadable):
                sent += 1
        for waiting in await self._store.pending_blocked_notices(self._batch_size):
            if await self._notify(waiting):
                sent += 1
        slow = await self._store.pending_progress_notices(
            _now() - timedelta(seconds=self._progress_after_seconds), self._batch_size
        )
        for working in slow:
            if await self._report_progress(working):
                sent += 1
        for pending in await self._store.pending_replies(self._batch_size):
            if await self._dispatch(pending):
                sent += 1
        return sent

    async def _report_progress(self, working: PendingProgress) -> bool:
        """Tell somebody their Run is taking a while — once.

        Settled whether or not it goes out, and never retried. There is no
        second notice to schedule, which is what keeps a ten-minute Run from
        producing thirty messages: the stamp is the whole mechanism, so
        there is no interval to tune and no counter to get wrong.
        """
        target = await self._store.delivery_target_for(working.session_id)
        recipient = self._sendable(target)
        if isinstance(recipient, str):
            logger.info(
                "channel progress not sent: run=%s reason=%s",
                working.run_id,
                recipient,
            )
            await self._store.settle_progress_notice(working.event_row_id, _now())
            return False
        try:
            secret = await self._resolve_secret(recipient.app_secret_ref)
            await self._senders(recipient.workspace_id).send_text(
                app_id=recipient.app_id,
                app_secret=secret,
                open_id=recipient.open_id,
                text=progress_note(),
                # A third distinct key for this delivery. Sharing any of
                # them would let Feishu's deduplication drop whichever
                # arrived second — and one of those is the answer.
                delivery_key=f"{working.event_row_id}{_PROGRESS_KEY_SUFFIX}",
            )
        except Exception:
            logger.warning(
                "channel progress failed: run=%s", working.run_id, exc_info=True
            )
            await self._store.settle_progress_notice(working.event_row_id, _now())
            return False
        await self._store.settle_progress_notice(working.event_row_id, _now())
        return True

    async def _refuse(self, unreadable: PendingRefusal) -> bool:
        """Tell somebody their photo could not be read.

        Its recipient comes off the delivery rather than from a session:
        there is no Run and often no conversation either, because a first
        message that happens to be a photo is exactly the case with nothing
        stored yet.

        Settled whether or not it goes out, and with no retry — same
        reasoning as the queue notice. A refusal that arrives late, after
        the person has already given up and typed the message again, is
        noise; one retried every scan is worse.
        """
        target = await self._store.binding_target(unreadable.binding_id)
        recipient = self._sendable(target)
        if isinstance(recipient, str):
            logger.info(
                "channel refusal not sent: binding=%s reason=%s",
                unreadable.binding_id,
                recipient,
            )
            await self._store.settle_reply(
                unreadable.event_row_id, recipient, _now()
            )
            return False
        try:
            secret = await self._resolve_secret(recipient.app_secret_ref)
            await self._senders(recipient.workspace_id).send_text(
                app_id=recipient.app_id,
                app_secret=secret,
                # From the delivery, not from `recipient` — `binding_target`
                # leaves that field empty precisely because a binding serves
                # everybody and cannot know who this one is for.
                open_id=unreadable.external_user_id,
                text=refusal_for(unreadable.kind),
                delivery_key=str(unreadable.event_row_id),
            )
        except Exception:
            logger.warning(
                "channel refusal failed: binding=%s",
                unreadable.binding_id,
                exc_info=True,
            )
            await self._store.settle_reply(
                unreadable.event_row_id, ReplyOutcome.REFUSED, _now()
            )
            return False
        await self._store.settle_reply(
            unreadable.event_row_id, ReplyOutcome.UNSUPPORTED_MESSAGE, _now()
        )
        return True

    async def _notify(self, waiting: PendingNotice) -> bool:
        """§19.2's status card, sent once.

        Settled whether or not it goes out, like a reply: a notice that only
        drained on success would be retried every scan for as long as the
        row survives the seven-day sweep.

        No retry bound of its own, because there is nothing to bound — this
        is told once and then the row is done. A notice that failed to send
        is a person who does not know they are queued, which is bad; a
        notice retried until it lands beside an answer that already arrived
        is worse.
        """
        target = await self._store.delivery_target_for(waiting.session_id)
        recipient = self._sendable(target)
        if isinstance(recipient, str):
            logger.info(
                "channel notice not sent: run=%s reason=%s", waiting.run_id, recipient
            )
            await self._store.settle_blocked_notice(waiting.event_row_id, _now())
            return False
        try:
            secret = await self._resolve_secret(recipient.app_secret_ref)
            await self._senders(recipient.workspace_id).send_card(
                app_id=recipient.app_id,
                app_secret=secret,
                open_id=recipient.open_id,
                card=blocked_card(waiting.notice, console_url=self._console_url),
                # Suffixed, so the notice and the answer for one delivery
                # never share a key. Sharing it would let Feishu's own
                # deduplication swallow the second — and the second is the
                # answer the person is actually waiting for.
                delivery_key=f"{waiting.event_row_id}{_NOTICE_KEY_SUFFIX}",
            )
        except Exception:
            logger.warning(
                "channel notice failed: run=%s", waiting.run_id, exc_info=True
            )
            await self._store.settle_blocked_notice(waiting.event_row_id, _now())
            return False
        await self._store.settle_blocked_notice(waiting.event_row_id, _now())
        return True

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
            await self._senders(recipient.workspace_id).send_text(
                app_id=recipient.app_id,
                app_secret=secret,
                open_id=recipient.open_id,
                text=text,
                # The delivery's own row id, stable across every attempt.
                # The retry bound has never been a delivery guarantee: a
                # failure after the request left this process cannot be told
                # apart from one before it, so only the channel can settle
                # whether a retry is a second message. A live tenant proved
                # the cost — five attempts, five real messages.
                delivery_key=str(pending.event_row_id),
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
            workspace_id=target.workspace_id,
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
    "ChannelSenders",
    "ReplyOutcome",
    "ReplyQueue",
]
