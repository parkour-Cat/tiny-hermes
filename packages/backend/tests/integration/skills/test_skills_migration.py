"""What `20260817_0014_skills.py` has to have put in the database.

The rest of the integration suite TRUNCATEs an already-migrated schema rather
than running alembic, so these assertions are the only place the migration
itself is read back. Every constraint checked here carries a rule that would
otherwise be a service-layer check somebody could forget to call.
"""

from collections.abc import Callable
from typing import Any

from sqlalchemy import Inspector, inspect
from sqlalchemy.ext.asyncio import AsyncEngine


async def _inspect[T](engine: AsyncEngine, read: Callable[[Inspector], T]) -> T:
    async with engine.connect() as connection:
        return await connection.run_sync(lambda sync: read(inspect(sync)))


def _columns(inspector: Inspector, table: str) -> dict[str, Any]:
    return {column["name"]: column for column in inspector.get_columns(table)}


async def _checks(engine: AsyncEngine, table: str) -> set[str]:
    return await _inspect(
        engine,
        lambda inspector: {
            item["name"] for item in inspector.get_check_constraints(table) if item["name"]
        },
    )


async def _uniques(engine: AsyncEngine, table: str) -> set[str]:
    constraints = await _inspect(
        engine,
        lambda inspector: {
            item["name"]
            for item in inspector.get_unique_constraints(table)
            if item["name"]
        },
    )
    indexes = await _inspect(engine, lambda inspector: inspector.get_indexes(table))
    return constraints | {
        str(index["name"])
        for index in indexes
        if index.get("unique") and index.get("name")
    }


async def test_the_four_tables_exist(engine: AsyncEngine) -> None:
    tables = await _inspect(engine, lambda inspector: set(inspector.get_table_names()))
    assert {"skills", "skill_versions", "skill_files", "skill_proposals"} <= tables


async def test_a_platform_skill_is_the_one_row_without_a_workspace(
    engine: AsyncEngine,
) -> None:
    """§9.3's exception, kept honest by a CHECK rather than by convention."""
    columns = await _inspect(engine, lambda inspector: _columns(inspector, "skills"))
    assert columns["workspace_id"]["nullable"] is True
    checks = await _checks(engine, "skills")
    assert "ck_skills_scope" in checks
    assert "ck_skills_scope_workspace" in checks


async def test_a_skill_name_is_taken_once_per_level(engine: AsyncEngine) -> None:
    """Partial indexes: PostgreSQL counts NULL workspaces as all distinct."""
    uniques = await _uniques(engine, "skills")
    assert "uq_skills_workspace_name" in uniques
    assert "uq_skills_platform_name" in uniques


async def test_the_same_content_cannot_become_a_second_version(
    engine: AsyncEngine,
) -> None:
    """Roadmap §5's exit check, as a database fact rather than a service check."""
    uniques = await _uniques(engine, "skill_versions")
    assert "uq_skill_versions_content" in uniques
    assert "uq_skill_versions_number" in uniques


async def test_a_version_records_where_it_came_from_and_what_the_scan_said(
    engine: AsyncEngine,
) -> None:
    columns = await _inspect(
        engine, lambda inspector: _columns(inspector, "skill_versions")
    )
    assert {
        "skill_id",
        "version_number",
        "content_hash",
        "manifest",
        "scan_findings",
        "source",
        "source_url",
        "source_ref",
        "status",
        "created_by",
        "created_at",
    } <= set(columns)
    checks = await _checks(engine, "skill_versions")
    assert "ck_skill_versions_source" in checks
    assert "ck_skill_versions_status" in checks


async def test_a_version_holds_each_path_once(engine: AsyncEngine) -> None:
    columns = await _inspect(engine, lambda inspector: _columns(inspector, "skill_files"))
    assert {"skill_version_id", "path", "content"} <= set(columns)
    assert "uq_skill_files_path" in await _uniques(engine, "skill_files")


async def test_a_decided_proposal_names_who_decided_it(engine: AsyncEngine) -> None:
    """Without this, a rejection written by a bug reads like one somebody meant."""
    checks = await _checks(engine, "skill_proposals")
    assert "ck_skill_proposals_status" in checks
    assert "ck_skill_proposals_origin" in checks
    assert "ck_skill_proposals_decision" in checks


async def test_a_run_may_be_told_that_a_skill_was_loaded(engine: AsyncEngine) -> None:
    """The CHECK 0006, 0008, 0012 and 0013 each had to widen, widened again."""
    clause = await _inspect(
        engine,
        lambda inspector: next(
            str(item["sqltext"])
            for item in inspector.get_check_constraints("run_events")
            if item["name"] == "ck_run_events_event_type"
        ),
    )
    assert "skill_loaded" in clause
