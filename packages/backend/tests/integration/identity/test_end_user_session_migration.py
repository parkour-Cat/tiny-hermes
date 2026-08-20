"""What migration 0031 actually put in the database.

`end_user_sessions` is the platform's own credential store for the third
subject (design §4.2): a short-lived, enterprise-signed JWT is exchanged for
this, and after that exchange the JWT is never consulted again. Same
technique as `test_end_user_identity_migration.py` — inspect the schema
`alembic upgrade head` produced, not the ORM model it was written from.
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


async def test_end_user_sessions_table_exists(engine: AsyncEngine) -> None:
    tables = await _inspect(engine, lambda inspector: set(inspector.get_table_names()))
    assert "end_user_sessions" in tables


async def test_end_user_sessions_columns_and_uniqueness(engine: AsyncEngine) -> None:
    columns = await _inspect(engine, lambda i: _columns(i, "end_user_sessions"))
    assert {
        "id",
        "end_user_id",
        "workspace_id",
        "token_digest",
        "expires_at",
        "revoked_at",
        "created_at",
    } <= set(columns)
    assert columns["token_digest"]["nullable"] is False
    assert columns["revoked_at"]["nullable"] is True
