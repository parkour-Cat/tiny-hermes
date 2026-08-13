"""Rows for workspace revisions and the upload lifecycle.

Every foreign key that crosses a module boundary carries ``workspace_id``
(design §6.1): a cross-tenant identifier must fail the constraint, not wait
for a query that remembers to filter. ``workspace_revisions`` deliberately has
no ``updated_at`` — a revision row is written once and never touched again.
"""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.session_workspace.domain.models import UploadKind, UploadStatus
from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin


def _in_enum(column: str, values: type[StrEnum]) -> str:
    listed = ", ".join(f"'{member.value}'" for member in values)
    return f"{column} IN ({listed})"


def _now() -> datetime:
    return datetime.now(UTC)


class WorkspaceRevisionRow(IdMixin, CreatedAtMixin, Base):
    """One immutable manifest: what the Session's files were at a commit."""

    __tablename__ = "workspace_revisions"
    __table_args__ = (
        UniqueConstraint("id", "workspace_id", name="uq_workspace_revisions_id_workspace"),
        CheckConstraint("total_bytes >= 0", name="ck_workspace_revisions_total_bytes"),
        CheckConstraint("object_count >= 0", name="ck_workspace_revisions_object_count"),
        CheckConstraint(
            "manifest_schema_version > 0",
            name="ck_workspace_revisions_schema_version",
        ),
        ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["sessions.id", "sessions.workspace_id"],
            name="fk_workspace_revisions_session",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["created_by_run_id", "workspace_id"],
            ["runs.id", "runs.workspace_id"],
            name="fk_workspace_revisions_run",
        ),
        ForeignKeyConstraint(
            ["parent_revision_id"],
            ["workspace_revisions.id"],
            name="fk_workspace_revisions_parent",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[UUID] = mapped_column(index=True)
    parent_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    manifest_schema_version: Mapped[int] = mapped_column(Integer)
    manifest_object_key: Mapped[str] = mapped_column(String(512))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    total_bytes: Mapped[int] = mapped_column(BigInteger)
    object_count: Mapped[int] = mapped_column(Integer)
    created_by_run_id: Mapped[UUID] = mapped_column()


class ObjectUploadRow(IdMixin, CreatedAtMixin, Base):
    """The database registration that exists before any object does.

    The row, its unique ``staging_prefix``, and its durable
    ``candidate_index_key`` are created while the upload is only an intention,
    which is what lets GC tell "orphaned garbage" from "commit in flight"
    without scanning the bucket.
    """

    __tablename__ = "object_uploads"
    __table_args__ = (
        UniqueConstraint("staging_prefix", name="uq_object_uploads_staging_prefix"),
        UniqueConstraint(
            "candidate_index_key", name="uq_object_uploads_candidate_index_key"
        ),
        UniqueConstraint(
            "candidate_revision_id", name="uq_object_uploads_candidate_revision"
        ),
        UniqueConstraint(
            "candidate_artifact_id", name="uq_object_uploads_candidate_artifact"
        ),
        CheckConstraint(_in_enum("status", UploadStatus), name="ck_object_uploads_status"),
        CheckConstraint(_in_enum("kind", UploadKind), name="ck_object_uploads_kind"),
        # `expired` may carry the reason too: it is the terminal state of an
        # abandoned upload, and the reason is history worth keeping.
        CheckConstraint(
            "(status IN ('abandoned', 'expired')) OR (abandon_reason IS NULL)",
            name="ck_object_uploads_abandon_reason",
        ),
        CheckConstraint(
            "total_bytes IS NULL OR total_bytes >= 0",
            name="ck_object_uploads_total_bytes",
        ),
        CheckConstraint(
            "object_count IS NULL OR object_count >= 0",
            name="ck_object_uploads_object_count",
        ),
        ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["sessions.id", "sessions.workspace_id"],
            name="fk_object_uploads_session",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["run_id", "workspace_id"],
            ["runs.id", "runs.workspace_id"],
            name="fk_object_uploads_run",
        ),
        ForeignKeyConstraint(
            ["base_revision_id", "workspace_id"],
            ["workspace_revisions.id", "workspace_revisions.workspace_id"],
            name="fk_object_uploads_base_revision",
        ),
        ForeignKeyConstraint(
            ["committed_revision_id", "workspace_id"],
            ["workspace_revisions.id", "workspace_revisions.workspace_id"],
            name="fk_object_uploads_committed_revision",
        ),
    )

    kind: Mapped[str] = mapped_column(String(16))
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[UUID] = mapped_column(index=True)
    run_id: Mapped[UUID] = mapped_column(index=True)
    base_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    candidate_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    candidate_artifact_id: Mapped[UUID | None] = mapped_column(nullable=True)
    staging_prefix: Mapped[str] = mapped_column(String(512))
    candidate_index_key: Mapped[str] = mapped_column(String(512))
    # Recorded at `finalizing` so GC can refuse to trust index bytes that are
    # not the ones the commit verified.
    candidate_index_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    cleanup_pending: Mapped[bool] = mapped_column(default=False, index=True)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    object_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    committed_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    abandon_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
