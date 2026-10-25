"""Published sites (§1.26, v2.26)."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin


class SitePublicationRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "site_publications"
    __table_args__ = (
        CheckConstraint(
            "visibility IN ('members', 'link')", name="ck_site_publications_visibility"
        ),
        CheckConstraint("length(token_sha256) = 64", name="ck_site_publications_token"),
        # Erasure wins over sharing: the revision goes with its session, and
        # the link with the revision.
        ForeignKeyConstraint(
            ["revision_id", "workspace_id"],
            ["workspace_revisions.id", "workspace_revisions.workspace_id"],
            name="fk_site_publications_revision",
            ondelete="CASCADE",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[UUID] = mapped_column()
    #: Which Run published it, for the audit and the console; not a key, so a
    #: Run's own lifecycle never blocks or takes the link.
    run_id: Mapped[UUID] = mapped_column()
    revision_id: Mapped[UUID] = mapped_column(index=True)
    root: Mapped[str] = mapped_column(String(512))
    title: Mapped[str | None] = mapped_column(String(100), nullable=True)
    #: The link's token is never stored: a database read hands out no links.
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    visibility: Mapped[str] = mapped_column(String(16), default="members")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SiteTicketUseRow(Base):
    """A members' ticket that has been exchanged, so it cannot be again."""

    __tablename__ = "site_ticket_uses"

    nonce: Mapped[str] = mapped_column(String(64), primary_key=True)
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
