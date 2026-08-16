from collections.abc import Callable
from typing import Any

from sqlalchemy import Inspector, inspect
from sqlalchemy.ext.asyncio import AsyncEngine


async def _inspect[T](engine: AsyncEngine, read: Callable[[Inspector], T]) -> T:
    async with engine.connect() as connection:
        return await connection.run_sync(lambda sync: read(inspect(sync)))


def _unique_sets(inspector: Inspector, table: str) -> set[frozenset[str]]:
    constraints: set[frozenset[str]] = {
        frozenset(item["column_names"])
        for item in inspector.get_unique_constraints(table)
    }
    for index in inspector.get_indexes(table):
        if index.get("unique"):
            names = [name for name in index["column_names"] if name is not None]
            constraints.add(frozenset(names))
    return constraints


def _columns(inspector: Inspector, table: str) -> dict[str, Any]:
    return {column["name"]: column for column in inspector.get_columns(table)}


async def test_machine_identity_tables_exist(engine: AsyncEngine) -> None:
    tables = await _inspect(engine, lambda inspector: set(inspector.get_table_names()))
    assert {"service_accounts", "api_keys"} <= tables


async def test_machine_identity_columns_and_uniqueness(engine: AsyncEngine) -> None:
    accounts = await _inspect(engine, lambda i: _columns(i, "service_accounts"))
    keys = await _inspect(engine, lambda i: _columns(i, "api_keys"))
    account_uniques = await _inspect(engine, lambda i: _unique_sets(i, "service_accounts"))
    key_uniques = await _inspect(engine, lambda i: _unique_sets(i, "api_keys"))
    key_indexes = await _inspect(
        engine, lambda i: {tuple(index["column_names"]) for index in i.get_indexes("api_keys")}
    )

    assert {
        "id",
        "workspace_id",
        "name",
        "role",
        "status",
        "created_by_user_id",
        "created_at",
    } <= set(accounts)
    assert {
        "id",
        "service_account_id",
        "token_digest",
        "prefix",
        "scopes",
        "agent_ids",
        "expires_at",
        "revoked_at",
        "created_at",
    } <= set(keys)
    assert "plaintext" not in keys
    assert "token" not in keys
    assert keys["token_digest"]["nullable"] is False
    assert keys["revoked_at"]["nullable"] is True
    assert frozenset({"workspace_id", "name"}) in account_uniques
    assert frozenset({"token_digest"}) in key_uniques
    assert ("prefix",) in key_indexes


async def test_machine_identity_check_constraints(engine: AsyncEngine) -> None:
    account_checks = await _inspect(
        engine,
        lambda i: {item["name"] for item in i.get_check_constraints("service_accounts")},
    )
    key_checks = await _inspect(
        engine,
        lambda i: {item["name"] for item in i.get_check_constraints("api_keys")},
    )
    assert "ck_service_accounts_role" in account_checks
    assert "ck_service_accounts_status" in account_checks
    assert "ck_api_keys_scopes" in key_checks
