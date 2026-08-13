"""Schema tests for migration 0007 — asked of PostgreSQL, not the metadata.

The metadata can say anything; what protects a tenant is what the database
refuses. Every assertion here inspects the live schema so a migration that
drifts from `tables.py` fails loudly instead of passing on model definitions
that were never applied.
"""

import re
from collections.abc import Callable

from sqlalchemy import Connection, inspect
from sqlalchemy.engine import Inspector
from sqlalchemy.engine.interfaces import ReflectedColumn
from sqlalchemy.ext.asyncio import AsyncEngine

UPLOAD_STATUSES = {
    "uploading",
    "finalizing",
    "ready",
    "committed",
    "abandoned",
    "expired",
}
UPLOAD_KINDS = {"workspace", "artifact"}
CLEANUP_TARGETS = {"paused_limit", "queued", "failed_conflict"}


async def _inspected[T](engine: AsyncEngine, reader: Callable[[Inspector], T]) -> T:
    def read(sync: Connection) -> T:
        return reader(inspect(sync))

    async with engine.connect() as connection:
        return await connection.run_sync(read)


def _columns(inspector: Inspector, table: str) -> dict[str, ReflectedColumn]:
    return {column["name"]: column for column in inspector.get_columns(table)}


def _quoted_values(inspector: Inspector, table: str, constraint: str) -> set[str]:
    """The exact value list a named check constraint accepts."""
    for check in inspector.get_check_constraints(table):
        if check["name"] == constraint:
            return set(re.findall(r"'([^']+)'", check["sqltext"]))
    raise AssertionError(f"missing check constraint {constraint} on {table}")


async def test_workspace_revision_rows_are_immutable_manifest_metadata(
    engine: AsyncEngine,
) -> None:
    columns = await _inspected(engine, lambda i: _columns(i, "workspace_revisions"))

    assert set(columns) == {
        "id",
        "workspace_id",
        "session_id",
        "parent_revision_id",
        "manifest_schema_version",
        "manifest_object_key",
        "manifest_sha256",
        "total_bytes",
        "object_count",
        "created_by_run_id",
        "created_at",
    }
    # No updated_at: a revision row is written once and never touched again.
    assert columns["parent_revision_id"]["nullable"]
    for name in set(columns) - {"parent_revision_id"}:
        assert not columns[name]["nullable"], f"{name} must be NOT NULL"


async def test_object_uploads_track_the_full_lifecycle(engine: AsyncEngine) -> None:
    columns = await _inspected(engine, lambda i: _columns(i, "object_uploads"))

    assert set(columns) == {
        "id",
        "kind",
        "workspace_id",
        "session_id",
        "run_id",
        "base_revision_id",
        "candidate_revision_id",
        "candidate_artifact_id",
        "staging_prefix",
        "candidate_index_key",
        "candidate_index_sha256",
        "final_object_key",
        "status",
        "cleanup_pending",
        "total_bytes",
        "object_count",
        "committed_revision_id",
        "abandon_reason",
        "expires_at",
        "created_at",
        "updated_at",
    }
    nullable = {
        name for name, column in columns.items() if column["nullable"]
    }
    assert nullable == {
        "base_revision_id",
        "candidate_revision_id",
        "candidate_artifact_id",
        "candidate_index_sha256",
        "final_object_key",
        "total_bytes",
        "object_count",
        "committed_revision_id",
        "abandon_reason",
    }


async def test_object_uploads_accept_only_known_statuses_and_kinds(
    engine: AsyncEngine,
) -> None:
    status_values = await _inspected(
        engine, lambda i: _quoted_values(i, "object_uploads", "ck_object_uploads_status")
    )
    kind_values = await _inspected(
        engine, lambda i: _quoted_values(i, "object_uploads", "ck_object_uploads_kind")
    )

    assert status_values == UPLOAD_STATUSES
    assert kind_values == UPLOAD_KINDS


async def test_staging_prefix_and_candidate_index_key_are_unique(
    engine: AsyncEngine,
) -> None:
    constraints = await _inspected(
        engine,
        lambda i: {
            tuple(entry["column_names"])
            for entry in i.get_unique_constraints("object_uploads")
        },
    )

    assert ("staging_prefix",) in constraints
    assert ("candidate_index_key",) in constraints


async def test_upload_and_revision_foreign_keys_carry_the_tenant(
    engine: AsyncEngine,
) -> None:
    """A cross-tenant identifier cannot be attached by accident (design §6.1)."""

    def links(inspector: Inspector, table: str) -> set[tuple[str, ...]]:
        return {
            (fk["referred_table"], *fk["constrained_columns"])
            for fk in inspector.get_foreign_keys(table)
        }

    upload_links = await _inspected(engine, lambda i: links(i, "object_uploads"))
    revision_links = await _inspected(engine, lambda i: links(i, "workspace_revisions"))

    assert ("sessions", "session_id", "workspace_id") in upload_links
    assert ("runs", "run_id", "workspace_id") in upload_links
    assert ("workspace_revisions", "base_revision_id", "workspace_id") in upload_links
    assert (
        "workspace_revisions",
        "committed_revision_id",
        "workspace_id",
    ) in upload_links
    assert ("sessions", "session_id", "workspace_id") in revision_links
    assert ("runs", "created_by_run_id", "workspace_id") in revision_links


async def test_cleanup_scans_have_the_indexes_they_walk(engine: AsyncEngine) -> None:
    def indexed(inspector: Inspector, table: str) -> set[tuple[str | None, ...]]:
        return {
            tuple(index["column_names"]) for index in inspector.get_indexes(table)
        }

    uploads = await _inspected(engine, lambda i: indexed(i, "object_uploads"))
    revisions = await _inspected(engine, lambda i: indexed(i, "workspace_revisions"))
    artifacts = await _inspected(engine, lambda i: indexed(i, "artifacts"))

    walked = ("workspace_id", "session_id", "run_id", "status", "expires_at", "cleanup_pending")
    for column in walked:
        assert (column,) in uploads, f"object_uploads needs an index on {column}"
    assert ("session_id",) in revisions
    assert ("workspace_id",) in revisions
    assert ("expires_at",) in artifacts
    assert ("run_id",) in artifacts


async def test_runs_carry_the_workspace_cleanup_intent(engine: AsyncEngine) -> None:
    columns = await _inspected(engine, lambda i: _columns(i, "runs"))
    target_values = await _inspected(
        engine,
        lambda i: _quoted_values(i, "runs", "ck_runs_workspace_cleanup_target"),
    )

    assert columns["workspace_cleanup_target"]["nullable"]
    assert columns["workspace_cleanup_sandbox_id"]["nullable"]
    assert target_values == CLEANUP_TARGETS


async def test_artifacts_carry_authorization_scope_and_retention(
    engine: AsyncEngine,
) -> None:
    columns = await _inspected(engine, lambda i: _columns(i, "artifacts"))

    assert set(columns) == {
        "id",
        "workspace_id",
        "session_id",
        "run_id",
        "object_key",
        "filename",
        "media_type",
        "size_bytes",
        "sha256",
        "truncated",
        "expires_at",
        "created_at",
    }
    for name in columns:
        assert not columns[name]["nullable"], f"{name} must be NOT NULL"
