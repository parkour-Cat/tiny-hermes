"""Workspace revisions, upload registrations, artifacts, and cleanup intent.

`object_uploads` is created alongside `workspace_revisions` in one migration
because neither is meaningful alone: a revision only ever appears by way of a
registered upload, and an upload row's whole purpose is the revision or
artifact it may become. The registration row must exist *before* any object
does — that ordering is what lets the collector tell orphaned garbage from a
commit in flight without scanning the bucket.

Every cross-module foreign key carries `workspace_id` (design §6.1), so a
cross-tenant identifier fails the constraint instead of waiting for a filter.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_0007"
down_revision: str | None = "20260811_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPLOAD_STATUSES = "'uploading', 'finalizing', 'ready', 'committed', 'abandoned', 'expired'"
UPLOAD_KINDS = "'workspace', 'artifact'"
CLEANUP_TARGETS = "'paused_limit', 'queued', 'failed_conflict'"


def upgrade() -> None:
    op.create_table(
        "workspace_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("parent_revision_id", sa.Uuid(), nullable=True),
        sa.Column("manifest_schema_version", sa.Integer(), nullable=False),
        sa.Column("manifest_object_key", sa.String(length=512), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("object_count", sa.Integer(), nullable=False),
        sa.Column("created_by_run_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_workspace_revisions_id_workspace"),
        sa.CheckConstraint("total_bytes >= 0", name="ck_workspace_revisions_total_bytes"),
        sa.CheckConstraint("object_count >= 0", name="ck_workspace_revisions_object_count"),
        sa.CheckConstraint(
            "manifest_schema_version > 0", name="ck_workspace_revisions_schema_version"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["sessions.id", "sessions.workspace_id"],
            name="fk_workspace_revisions_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_run_id", "workspace_id"],
            ["runs.id", "runs.workspace_id"],
            name="fk_workspace_revisions_run",
        ),
        sa.ForeignKeyConstraint(
            ["parent_revision_id"],
            ["workspace_revisions.id"],
            name="fk_workspace_revisions_parent",
        ),
    )
    op.create_index(
        op.f("ix_workspace_revisions_workspace_id"),
        "workspace_revisions",
        ["workspace_id"],
    )
    op.create_index(
        op.f("ix_workspace_revisions_session_id"), "workspace_revisions", ["session_id"]
    )

    op.create_table(
        "object_uploads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("base_revision_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_revision_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("staging_prefix", sa.String(length=512), nullable=False),
        sa.Column("candidate_index_key", sa.String(length=512), nullable=False),
        sa.Column("final_object_key", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("cleanup_pending", sa.Boolean(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("object_count", sa.Integer(), nullable=True),
        sa.Column("committed_revision_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("staging_prefix", name="uq_object_uploads_staging_prefix"),
        sa.UniqueConstraint(
            "candidate_index_key", name="uq_object_uploads_candidate_index_key"
        ),
        sa.UniqueConstraint(
            "candidate_revision_id", name="uq_object_uploads_candidate_revision"
        ),
        sa.UniqueConstraint(
            "candidate_artifact_id", name="uq_object_uploads_candidate_artifact"
        ),
        sa.CheckConstraint(
            f"status IN ({UPLOAD_STATUSES})", name="ck_object_uploads_status"
        ),
        sa.CheckConstraint(f"kind IN ({UPLOAD_KINDS})", name="ck_object_uploads_kind"),
        sa.CheckConstraint(
            "total_bytes IS NULL OR total_bytes >= 0",
            name="ck_object_uploads_total_bytes",
        ),
        sa.CheckConstraint(
            "object_count IS NULL OR object_count >= 0",
            name="ck_object_uploads_object_count",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["sessions.id", "sessions.workspace_id"],
            name="fk_object_uploads_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "workspace_id"],
            ["runs.id", "runs.workspace_id"],
            name="fk_object_uploads_run",
        ),
        sa.ForeignKeyConstraint(
            ["base_revision_id", "workspace_id"],
            ["workspace_revisions.id", "workspace_revisions.workspace_id"],
            name="fk_object_uploads_base_revision",
        ),
        sa.ForeignKeyConstraint(
            ["committed_revision_id", "workspace_id"],
            ["workspace_revisions.id", "workspace_revisions.workspace_id"],
            name="fk_object_uploads_committed_revision",
        ),
    )
    for column in (
        "workspace_id",
        "session_id",
        "run_id",
        "status",
        "expires_at",
        "cleanup_pending",
    ):
        op.create_index(
            op.f(f"ix_object_uploads_{column}"), "object_uploads", [column]
        )

    op.create_table(
        "artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=120), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_artifacts_size_bytes"),
        sa.CheckConstraint("length(sha256) = 64", name="ck_artifacts_sha256"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["sessions.id", "sessions.workspace_id"],
            name="fk_artifacts_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "workspace_id"],
            ["runs.id", "runs.workspace_id"],
            name="fk_artifacts_run",
        ),
    )
    for column in ("workspace_id", "session_id", "run_id", "expires_at"):
        op.create_index(op.f(f"ix_artifacts_{column}"), "artifacts", [column])

    op.add_column(
        "runs", sa.Column("workspace_cleanup_target", sa.String(length=20), nullable=True)
    )
    op.add_column(
        "runs", sa.Column("workspace_cleanup_sandbox_id", sa.Uuid(), nullable=True)
    )
    op.create_check_constraint(
        "ck_runs_workspace_cleanup_target",
        "runs",
        "workspace_cleanup_target IS NULL OR "
        f"workspace_cleanup_target IN ({CLEANUP_TARGETS})",
    )


def downgrade() -> None:
    op.drop_constraint("ck_runs_workspace_cleanup_target", "runs", type_="check")
    op.drop_column("runs", "workspace_cleanup_sandbox_id")
    op.drop_column("runs", "workspace_cleanup_target")
    op.drop_table("artifacts")
    op.drop_table("object_uploads")
    op.drop_table("workspace_revisions")
