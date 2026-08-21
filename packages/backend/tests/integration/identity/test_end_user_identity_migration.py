"""What migration 0030 actually put in the database.

Mirrors `test_machine_identity_migration.py`'s technique: inspect the real
schema rather than trust the SQLAlchemy metadata the migration was written
from, because the thing that must be true is what `alembic upgrade head`
produced, not what the ORM model says it produced.
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


def _unique_sets(inspector: Inspector, table: str) -> set[frozenset[str]]:
    constraints: set[frozenset[str]] = {
        frozenset(item["column_names"]) for item in inspector.get_unique_constraints(table)
    }
    for index in inspector.get_indexes(table):
        if index.get("unique"):
            names = [name for name in index["column_names"] if name is not None]
            constraints.add(frozenset(names))
    return constraints


async def test_end_user_identity_tables_exist(engine: AsyncEngine) -> None:
    tables = await _inspect(engine, lambda inspector: set(inspector.get_table_names()))
    assert {"end_users", "external_identities", "channel_issuers"} <= tables


async def test_end_users_columns_carry_no_identifiable_information(engine: AsyncEngine) -> None:
    columns = await _inspect(engine, lambda i: _columns(i, "end_users"))
    assert set(columns) == {"id", "workspace_id", "created_at", "erased_at"}
    assert columns["erased_at"]["nullable"] is True


async def test_external_identities_columns_and_uniqueness(engine: AsyncEngine) -> None:
    columns = await _inspect(engine, lambda i: _columns(i, "external_identities"))
    uniques = await _inspect(engine, lambda i: _unique_sets(i, "external_identities"))

    assert {
        "id",
        "workspace_id",
        "channel",
        "external_user_id",
        "end_user_id",
        "profile",
        "created_at",
    } <= set(columns)
    assert columns["profile"]["nullable"] is True
    # §282: the identity the whole design rests on.
    assert frozenset({"workspace_id", "channel", "external_user_id"}) in uniques


async def test_channel_issuers_columns_and_uniqueness(engine: AsyncEngine) -> None:
    columns = await _inspect(engine, lambda i: _columns(i, "channel_issuers"))
    uniques = await _inspect(engine, lambda i: _unique_sets(i, "channel_issuers"))
    checks = await _inspect(
        engine, lambda i: {item["name"] for item in i.get_check_constraints("channel_issuers")}
    )

    assert {
        "id",
        "workspace_id",
        "channel",
        "issuer",
        "public_key",
        "jwks_url",
        "allowed_origins",
        "status",
        "created_by",
        "created_at",
    } <= set(columns)
    assert frozenset({"workspace_id", "channel", "issuer"}) in uniques
    assert "ck_channel_issuers_status" in checks
    assert "ck_channel_issuers_key_source" in checks


async def test_caller_type_check_constraints_admit_end_user(engine: AsyncEngine) -> None:
    """0030 widens two CHECKs generated from `CallerType` rather than one.

    The definition text is the one thing `get_check_constraints` actually
    reports, so this asserts on the SQL fragment rather than on a name — a
    dropped-and-recreated constraint that kept the old two-value clause would
    still have the right name.
    """
    session_checks = await _inspect(
        engine,
        lambda i: {item["name"]: item["sqltext"] for item in i.get_check_constraints("sessions")},
    )
    idempotency_checks = await _inspect(
        engine,
        lambda i: {
            item["name"]: item["sqltext"] for item in i.get_check_constraints("idempotency_records")
        },
    )

    assert "'end_user'" in session_checks["ck_sessions_caller_type"]
    assert "'end_user'" in idempotency_checks["ck_idempotency_records_caller_type"]
