"""Four tables. Skill content lives in Postgres rows, not in object storage.

Skills are text, and small text at that (§1's ceilings put a whole version
under a megabyte). Keeping the bodies here means immutability, the content
hash and per-version rollback are one foreign key rather than two sources of
truth that have to be reconciled after a failed write. Artifacts and workspace
checkpoints go to object storage because they are neither small nor text.

The uniqueness that matters is `(skill_id, content_hash)`: importing the same
content twice does not make a second version, and that is a database fact
rather than a check the service layer has to remember to run first.
"""

from datetime import datetime
from enum import StrEnum
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
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.identity.infrastructure import tables as identity_tables
from tiny_hermes.runs.infrastructure import tables as run_tables
from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin
from tiny_hermes.skills.domain.models import (
    ProposalOrigin,
    ProposalStatus,
    SkillScope,
    SkillSource,
    SkillVersionStatus,
)
from tiny_hermes.skills.domain.package import NAME_MAX_LENGTH
from tiny_hermes.tenancy.infrastructure import tables as tenancy_tables

#: The other modules whose tables this one's foreign keys name.
#:
#: Imported for their side effect — defining those tables on the shared
#: metadata — and named here so neither linter reads them as dead. SQLAlchemy
#: resolves a foreign key by table *name*, at flush time, against whatever has
#: been registered; a name it cannot find raises `NoReferencedTableError`. The
#: API never saw one because its routers import every module anyway, and the
#: Worker met it the first time an Agent proposed a skill change. This file is
#: what knows where its keys point, so this is where they are pulled in.
REFERENCED_TABLE_MODULES = (identity_tables, run_tables, tenancy_tables)



def _in_enum(column: str, values: type[StrEnum]) -> str:
    listed = ", ".join(f"'{member.value}'" for member in values)
    return f"{column} IN ({listed})"


class SkillRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "skills"
    __table_args__ = (
        CheckConstraint(_in_enum("scope", SkillScope), name="ck_skills_scope"),
        CheckConstraint(
            "(scope = 'platform' AND workspace_id IS NULL) OR "
            "(scope = 'workspace' AND workspace_id IS NOT NULL)",
            name="ck_skills_scope_workspace",
        ),
        # Partial, because the two levels name independently: a workspace may
        # have a `release-notes` of its own while the platform also ships one.
        Index(
            "uq_skills_workspace_name",
            "workspace_id",
            "name",
            unique=True,
            postgresql_where=text("scope = 'workspace'"),
        ),
        Index(
            "uq_skills_platform_name",
            "name",
            unique=True,
            postgresql_where=text("scope = 'platform'"),
        ),
    )

    scope: Mapped[str] = mapped_column(String(32))
    workspace_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH))
    # `use_alter`, because `skill_versions` points back at this table. Nothing
    # published reads this column; see `Skill.current_version_id`.
    current_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("skill_versions.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SkillVersionRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "skill_versions"
    __table_args__ = (
        UniqueConstraint("skill_id", "version_number", name="uq_skill_versions_number"),
        # Red line: the same content does not become a second version.
        UniqueConstraint("skill_id", "content_hash", name="uq_skill_versions_content"),
        CheckConstraint("version_number > 0", name="ck_skill_versions_number_positive"),
        CheckConstraint(_in_enum("source", SkillSource), name="ck_skill_versions_source"),
        CheckConstraint(
            _in_enum("status", SkillVersionStatus), name="ck_skill_versions_status"
        ),
    )

    skill_id: Mapped[UUID] = mapped_column(
        ForeignKey("skills.id", ondelete="CASCADE"), index=True
    )
    version_number: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    #: Stored, not recomputed on read. A reviewer approved a version against
    #: what the scan said that day, and a later rule change must not silently
    #: rewrite the record of what they were shown.
    scan_findings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(32))
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), default=SkillVersionStatus.ACTIVE.value
    )
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class SkillFileRow(IdMixin, Base):
    """One file of one version. `Text`, because §10.4 accepts UTF-8 text only."""

    __tablename__ = "skill_files"
    __table_args__ = (
        UniqueConstraint("skill_version_id", "path", name="uq_skill_files_path"),
    )

    skill_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("skill_versions.id", ondelete="CASCADE"), index=True
    )
    path: Mapped[str] = mapped_column(String(512))
    content: Mapped[str] = mapped_column(Text)


class SkillProposalRow(IdMixin, CreatedAtMixin, Base):
    """A suggestion, with its files inline.

    Not in `skill_files`: that table's rows belong to an immutable version, and
    a proposal is the one thing here that may be rejected and never become one.
    Keeping them apart means no query has to ask which rows are real yet.
    """

    __tablename__ = "skill_proposals"
    __table_args__ = (
        CheckConstraint(_in_enum("origin", ProposalOrigin), name="ck_skill_proposals_origin"),
        CheckConstraint(_in_enum("status", ProposalStatus), name="ck_skill_proposals_status"),
        CheckConstraint(
            "(status = 'pending' AND decided_by IS NULL AND decided_at IS NULL) OR "
            "(status <> 'pending' AND decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_skill_proposals_decision",
        ),
    )

    workspace_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True, index=True
    )
    skill_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("skills.id", ondelete="CASCADE"), nullable=True, index=True
    )
    base_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("skill_versions.id", ondelete="SET NULL"), nullable=True
    )
    files: Mapped[list[Any]] = mapped_column(JSON)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    scan_findings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    origin: Mapped[str] = mapped_column(String(32))
    origin_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default=ProposalStatus.PENDING.value)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    decided_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
