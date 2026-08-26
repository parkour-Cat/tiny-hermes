"""Where a channel publishes an Agent, and what it has already delivered.

See migration `20260822_0036` for why `channel_events` exists for its unique
constraint rather than for its rows.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin


class ChannelBindingRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "channel_bindings"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "channel", "agent_id", name="uq_channel_bindings_target"
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')", name="ck_channel_bindings_status"
        ),
        CheckConstraint(
            "channel <> 'feishu' OR encrypt_key_ref IS NOT NULL",
            name="ck_channel_bindings_feishu_is_encrypted",
        ),
        Index("ix_channel_bindings_workspace", "workspace_id", "channel"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    channel: Mapped[str] = mapped_column(String(32))
    agent_id: Mapped[UUID] = mapped_column(ForeignKey("agents.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    #: A reference, never the key — see migration 0037. Nullable because a
    #: `web` or `api` binding has no such key; the CHECK above is what makes
    #: it required for the channel that does.
    encrypt_key_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    app_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: The app secret, by reference (migration 0039). Nullable with no
    #: CHECK, unlike `encrypt_key_ref`: a receive-only binding is a valid
    #: state — §929's drill needs one — and the outbound sender refuses when
    #: this is absent rather than the schema forbidding the binding.
    app_secret_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)


class ChannelEventRow(IdMixin, Base):
    __tablename__ = "channel_events"
    __table_args__ = (
        UniqueConstraint(
            "channel_binding_id", "channel_event_id", name="uq_channel_events_delivery"
        ),
        CheckConstraint("reply_attempts >= 0", name="ck_channel_events_reply_attempts"),
        # The sweep's only access path: everything older than a cutoff.
        Index("ix_channel_events_received_at", "received_at"),
        # The reply scan's, and it matches that predicate exactly — see
        # migration 0040.
        Index(
            "ix_channel_events_awaiting_reply",
            "run_id",
            postgresql_where=text("run_id IS NOT NULL AND replied_at IS NULL"),
        ),
        # The notice scan's, matching its predicate exactly — migration 0041.
        Index(
            "ix_channel_events_awaiting_notice",
            "received_at",
            postgresql_where=text(
                "blocked_notice IS NOT NULL AND blocked_notified_at IS NULL"
            ),
        ),
        # The opening card's — migration 0044.
        Index(
            "ix_channel_events_awaiting_card",
            "received_at",
            postgresql_where=text(
                "run_id IS NOT NULL AND card_message_id IS NULL"
                " AND replied_at IS NULL AND card_attempted_at IS NULL"
            ),
        ),
        # The progress scan's — migration 0043.
        Index(
            "ix_channel_events_awaiting_progress",
            "received_at",
            postgresql_where=text(
                "run_id IS NOT NULL AND replied_at IS NULL"
                " AND progress_notified_at IS NULL AND blocked_notice IS NULL"
            ),
        ),
        # The refusal scan's — migration 0042.
        Index(
            "ix_channel_events_awaiting_refusal",
            "received_at",
            postgresql_where=text(
                "unsupported_kind IS NOT NULL AND replied_at IS NULL"
            ),
        ),
        # The command-receipt scan's, matching its predicate exactly —
        # migration 0048.
        Index(
            "ix_channel_events_awaiting_command_receipt",
            "received_at",
            postgresql_where=text(
                "command_receipt IS NOT NULL AND replied_at IS NULL"
            ),
        ),
    )

    channel_binding_id: Mapped[UUID] = mapped_column(
        ForeignKey("channel_bindings.id", ondelete="CASCADE")
    )
    channel_event_id: Mapped[str] = mapped_column(String(200))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Nullable because the row is claimed *before* the Run exists — claiming
    #: first is what makes the claim exclusive. A Run created before the claim
    #: would be the duplicate this table exists to prevent.
    #:
    #: It is also the outbound queue's key: a row with a `run_id` and no
    #: `replied_at` is a delivery still owed an answer. That is why the
    #: column stopped being decorative — it went unwritten for a whole
    #: milestone and nothing noticed, because nothing read it.
    run_id: Mapped[UUID | None] = mapped_column(nullable=True)
    #: Set once the reply is settled — sent, or deliberately not sent. Not
    #: "sent at": a binding that was disabled or has no credential settles
    #: too, and a queue that only drains on success never drains.
    replied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reply_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: How it settled, for whoever is asked why a reply never arrived. See
    #: migration 0040 for why this exists rather than only a log line.
    reply_note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: §497's facts as they were **at the moment this message landed**, or
    #: NULL for the ordinary unblocked delivery. Stored rather than
    #: re-derived because the inbound moment is the only accurate one — see
    #: migration 0041.
    blocked_notice: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    #: Separate from `replied_at` on purpose: one delivery produces two
    #: sends, and a shared stamp would settle the row before the answer the
    #: person is waiting for had been written.
    blocked_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: The message type this build could not read, or NULL for a readable
    #: delivery. It reuses `replied_at`/`reply_note` to settle, because a
    #: refusal is an answer — see migration 0042.
    unsupported_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Who to answer. Kept here rather than looked up, because a first-ever
    #: message that happens to be a photo has no `channel_conversations`
    #: row — and that person is the one most in need of an answer.
    unsupported_open_id: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    #: When the sender was told the Run is taking a while. Its own stamp for
    #: the same reason as `blocked_notified_at`, and a stamp rather than a
    #: counter because it is said exactly once — migration 0043.
    progress_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Feishu's id for the card this delivery opened with, so later stages
    #: rewrite it rather than adding messages. NULL means there is nothing to
    #: patch and the answer goes as a new message — migration 0044.
    card_message_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: When the opening card was attempted, whether or not an id came back.
    #: Separate from `card_message_id` so a send that produced no id still
    #: leaves the scan — otherwise it would re-send a card every second.
    card_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: A `/undo` or `/new` produces no Run, so it never reaches
    #: `pending_replies`'s join to `runs`. Its own document, for its own
    #: scan — see migration 0048 for why this is not `blocked_notice` or
    #: `unsupported_kind` reused.
    command_receipt: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    #: Who to answer, kept here for the same reason as `unsupported_open_id`:
    #: a command does not create a `channel_conversations` row on its own,
    #: so the scan has nowhere else to find the sender.
    command_open_id: Mapped[str | None] = mapped_column(String(120), nullable=True)


class ChannelConversationRow(IdMixin, CreatedAtMixin, Base):
    """See migration `20260822_0038` for why this is keyed by participant
    rather than by the channel's own chat id, and why it does not live in
    `channel_events`."""

    __tablename__ = "channel_conversations"
    __table_args__ = (
        UniqueConstraint(
            "channel_binding_id",
            "external_user_id",
            name="uq_channel_conversations_participant",
        ),
    )

    channel_binding_id: Mapped[UUID] = mapped_column(
        ForeignKey("channel_bindings.id", ondelete="CASCADE")
    )
    external_user_id: Mapped[str] = mapped_column(String(200))
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE")
    )
