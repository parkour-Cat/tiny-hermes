"""The artifacts table: Run results with their own retention clock.

Download authorization rechecks Workspace membership on every request
(design §6.4); the composite foreign keys make sure the row itself can never
point at another tenant's Session or Run in the first place.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin


class ArtifactRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="ck_artifacts_size_bytes"),
        CheckConstraint("length(sha256) = 64", name="ck_artifacts_sha256"),
        ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["sessions.id", "sessions.workspace_id"],
            name="fk_artifacts_session",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["run_id", "workspace_id"],
            ["runs.id", "runs.workspace_id"],
            name="fk_artifacts_run",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[UUID] = mapped_column(index=True)
    run_id: Mapped[UUID] = mapped_column(index=True)
    object_key: Mapped[str] = mapped_column(String(512))
    filename: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    truncated: Mapped[bool] = mapped_column(default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ArtifactGrantRow(IdMixin, CreatedAtMixin, Base):
    """One Run's permission to read one Artifact it did not produce.

    §13's eighth clause is that files move between a parent and a child as
    **authorizations**, never through a shared directory — and an
    authorization is a row rather than a copy. Two consequences the shape is
    chosen for.

    A grant is per Run, not per Agent and not per Session. The question a read
    asks is "may *this* Run see this file", and a grant that outlived the Run
    it was made for would let a later Run of the same Agent read something
    nobody passed it.

    `reason` records which direction it went — a parent handing a file down or
    a child's output being handed up. It is not consulted when a read is
    checked; it is there so that "why can this Run read this" stays answerable
    afterwards, which a bare pair of ids could not answer.
    """

    __tablename__ = "artifact_grants"
    __table_args__ = (
        # One grant per pair. Granting twice is ordinary — a parent may pass
        # the same file to two children, and a retried delivery re-grants —
        # and two rows would make "is this granted" a counting question.
        UniqueConstraint("artifact_id", "run_id", name="uq_artifact_grants_pair"),
        CheckConstraint(
            "reason IN ('delegated_down', 'delivered_up')",
            name="ck_artifact_grants_reason",
        ),
        ForeignKeyConstraint(
            ["artifact_id"],
            ["artifacts.id"],
            name="fk_artifact_grants_artifact",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["run_id", "workspace_id"],
            ["runs.id", "runs.workspace_id"],
            name="fk_artifact_grants_run",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    artifact_id: Mapped[UUID] = mapped_column(index=True)
    #: The Run that may read it. Never the Agent: see the class docstring.
    run_id: Mapped[UUID] = mapped_column(index=True)
    reason: Mapped[str] = mapped_column(String(20))
