from collections.abc import Callable
from typing import Any

from sqlalchemy import Inspector, inspect
from sqlalchemy.ext.asyncio import AsyncEngine


async def _inspect[T](engine: AsyncEngine, read: Callable[[Inspector], T]) -> T:
    async with engine.connect() as connection:
        return await connection.run_sync(lambda sync: read(inspect(sync)))


def _columns(inspector: Inspector, table: str) -> dict[str, Any]:
    return {column["name"]: column for column in inspector.get_columns(table)}


async def test_secrets_table_exists(engine: AsyncEngine) -> None:
    tables = await _inspect(engine, lambda inspector: set(inspector.get_table_names()))
    assert "secrets" in tables


async def test_secrets_columns_exclude_plaintext(engine: AsyncEngine) -> None:
    columns = await _inspect(engine, lambda inspector: _columns(inspector, "secrets"))
    assert {
        "id",
        "name",
        "scope",
        "workspace_id",
        "status",
        "mask",
        "ciphertext",
        "nonce",
        "wrapped_dek",
        "wrap_nonce",
        "key_id",
        "created_at",
        "updated_at",
    } <= set(columns)
    assert "plaintext" not in columns
    assert "value" not in columns
    assert columns["workspace_id"]["nullable"] is True
    assert columns["ciphertext"]["nullable"] is False
    assert columns["wrapped_dek"]["nullable"] is False


async def test_secrets_check_constraints_and_partial_uniques(engine: AsyncEngine) -> None:
    checks = await _inspect(
        engine,
        lambda inspector: {item["name"] for item in inspector.get_check_constraints("secrets")},
    )
    indexes = await _inspect(
        engine, lambda inspector: inspector.get_indexes("secrets")
    )
    unique_names = {
        index["name"] for index in indexes if index.get("unique") and index.get("name")
    }
    assert "ck_secrets_scope" in checks
    assert "ck_secrets_status" in checks
    assert "ck_secrets_scope_workspace" in checks
    assert "uq_secrets_workspace_name" in unique_names
    assert "uq_secrets_platform_name" in unique_names
