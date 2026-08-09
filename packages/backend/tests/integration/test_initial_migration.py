from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine


async def test_initial_migration_creates_foundation_tables(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))

    assert {
        "users",
        "auth_identities",
        "auth_sessions",
        "workspaces",
        "memberships",
        "audit_events",
        "alembic_version",
    } <= tables


async def test_initial_migration_contains_identity_safety_columns(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        user_columns = await connection.run_sync(
            lambda sync: {column["name"] for column in inspect(sync).get_columns("users")}
        )
        session_columns = await connection.run_sync(
            lambda sync: {column["name"] for column in inspect(sync).get_columns("auth_sessions")}
        )

    assert "is_platform_admin" in user_columns
    assert {"token_digest", "csrf_digest", "expires_at", "revoked_at"} <= session_columns
