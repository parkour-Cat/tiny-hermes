"""The skill catalog: skills, immutable versions, their files, and proposals.

Two changes in one revision, for the reason 0013 gave: `run_events.event_type`
gains `skill_loaded` here rather than in the revision that first writes one.
The producer arrives with progressive loading; an allowed value nobody writes
yet is inert, and bundling it costs one downtime window instead of two.

`skills.current_version_id` points at `skill_versions`, which points back at
`skills`, so the foreign key is added after both tables exist rather than
inside the first `create_table`.

File bodies are columns in `skill_files` rather than objects in storage.
Skills are small text, and one table makes immutability, the content hash and
per-version rollback a single foreign key instead of two stores to reconcile.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260817_0014"
down_revision: str | None = "20260817_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIGNALS = (
    "lease_acquired",
    "slice_ended",
    "pause_requested",
    "resume_requested",
    "cancel_requested",
    "safe_pause_reached",
    "safe_cancel_started",
    "safe_cancel_finished",
    "approval_requested",
    "approval_approved",
    "approval_paused",
    "external_wait_started",
    "external_ready",
    "external_paused",
    "completed",
    "failed",
    "interrupted",
    "recovery_approved",
    "recovery_failed",
    "limit_cleanup_confirmed",
)

WORKSPACE_FACTS = (
    "workspace_limit_exceeded",
    "workspace_conflict",
    "workspace_checkpoint_failed",
    "workspace_storage_unavailable",
    "workspace_integrity_failed",
    "workspace_entry_not_supported",
)

FIXED_BEFORE = (
    "'run_created', 'run_retry_derived', 'session_head_repaired', "
    "'run_limit_reached', 'goal_verdict', 'context_trimmed', 'context_compacted', "
    "'sandbox_cache_reset'"
)
FIXED_AFTER = (
    "'run_created', 'run_retry_derived', 'session_head_repaired', "
    "'run_limit_reached', 'goal_verdict', 'context_trimmed', 'context_compacted', "
    "'skill_loaded', 'sandbox_cache_reset'"
)


def _clause(fixed: str) -> str:
    signal_names = ", ".join(f"'run_{name}'" for name in SIGNALS)
    fact_names = "".join(f", '{name}'" for name in WORKSPACE_FACTS)
    return f"event_type IN ({fixed}, {signal_names}{fact_names})"


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("scope IN ('platform', 'workspace')", name="ck_skills_scope"),
        sa.CheckConstraint(
            "(scope = 'platform' AND workspace_id IS NULL) OR "
            "(scope = 'workspace' AND workspace_id IS NOT NULL)",
            name="ck_skills_scope_workspace",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_skills_workspace_id", "skills", ["workspace_id"])
    # Partial: PostgreSQL treats NULLs as distinct, so a plain unique constraint
    # on `(workspace_id, name)` would not name platform skills at all.
    op.create_index(
        "uq_skills_workspace_name",
        "skills",
        ["workspace_id", "name"],
        unique=True,
        postgresql_where=sa.text("scope = 'workspace'"),
    )
    op.create_index(
        "uq_skills_platform_name",
        "skills",
        ["name"],
        unique=True,
        postgresql_where=sa.text("scope = 'platform'"),
    )

    op.create_table(
        "skill_versions",
        sa.Column("skill_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("scan_findings", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("source_ref", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "version_number > 0", name="ck_skill_versions_number_positive"
        ),
        sa.CheckConstraint(
            "source IN ('upload', 'git', 'proposal')", name="ck_skill_versions_source"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'withdrawn')", name="ck_skill_versions_status"
        ),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "skill_id", "version_number", name="uq_skill_versions_number"
        ),
        # Importing the same content twice does not make a second version, and
        # that is a fact the database keeps rather than a check a caller runs.
        sa.UniqueConstraint(
            "skill_id", "content_hash", name="uq_skill_versions_content"
        ),
    )
    op.create_index("ix_skill_versions_skill_id", "skill_versions", ["skill_id"])
    op.create_foreign_key(
        "fk_skills_current_version",
        "skills",
        "skill_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "skill_files",
        sa.Column("skill_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["skill_version_id"], ["skill_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("skill_version_id", "path", name="uq_skill_files_path"),
    )
    op.create_index("ix_skill_files_skill_version_id", "skill_files", ["skill_version_id"])

    op.create_table(
        "skill_proposals",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("skill_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("base_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("files", sa.JSON(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("scan_findings", sa.JSON(), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("origin_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decided_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "origin IN ('agent', 'human')", name="ck_skill_proposals_origin"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_skill_proposals_status",
        ),
        # A decided proposal names who decided it. Without this a rejection
        # written by a bug is indistinguishable from one somebody stands behind.
        sa.CheckConstraint(
            "(status = 'pending' AND decided_by IS NULL AND decided_at IS NULL) OR "
            "(status <> 'pending' AND decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_skill_proposals_decision",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["base_version_id"], ["skill_versions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["origin_run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_skill_proposals_workspace_id", "skill_proposals", ["workspace_id"])
    op.create_index("ix_skill_proposals_skill_id", "skill_proposals", ["skill_id"])

    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint(
        "ck_run_events_event_type", "run_events", _clause(FIXED_AFTER)
    )


def downgrade() -> None:
    # A loaded skill body explains why a round's request looked the way it did.
    # Dropping the events loses the explanation; the Run's own history is in the
    # transitions and the transcript, and neither is touched. Literal, never
    # caller input — the shape 0008, 0012 and 0013 used.
    op.execute("DELETE FROM run_events WHERE event_type IN ('skill_loaded')")
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint(
        "ck_run_events_event_type", "run_events", _clause(FIXED_BEFORE)
    )

    op.drop_index("ix_skill_proposals_skill_id", table_name="skill_proposals")
    op.drop_index("ix_skill_proposals_workspace_id", table_name="skill_proposals")
    op.drop_table("skill_proposals")
    op.drop_index("ix_skill_files_skill_version_id", table_name="skill_files")
    op.drop_table("skill_files")
    op.drop_constraint("fk_skills_current_version", "skills", type_="foreignkey")
    op.drop_index("ix_skill_versions_skill_id", table_name="skill_versions")
    op.drop_table("skill_versions")
    op.drop_index("uq_skills_platform_name", table_name="skills")
    op.drop_index("uq_skills_workspace_name", table_name="skills")
    op.drop_index("ix_skills_workspace_id", table_name="skills")
    op.drop_table("skills")
