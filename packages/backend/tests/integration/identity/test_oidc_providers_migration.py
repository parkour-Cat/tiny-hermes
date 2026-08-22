"""What migration 0035 actually put in the database.

OIDC login design §1's one schema change plus the two new tables it needs.
Same technique as every other migration test in this package: inspect the
schema `alembic upgrade head` produced, not the ORM model it was written
from.
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


async def test_auth_identities_password_hash_became_nullable(engine: AsyncEngine) -> None:
    columns = await _inspect(engine, lambda i: _columns(i, "auth_identities"))
    # An OIDC identity has no password to hash — see the migration's own
    # docstring for why `AuthService.login` still refuses a null hash rather
    # than trusting this constraint alone.
    assert columns["password_hash"]["nullable"] is True


async def test_oidc_providers_table_exists_with_a_unique_issuer(engine: AsyncEngine) -> None:
    columns = await _inspect(engine, lambda i: _columns(i, "oidc_providers"))
    for name in (
        "id",
        "issuer",
        "client_id",
        "client_secret_ref",
        "discovery_url",
        "scopes",
        "status",
        "created_by",
        "created_at",
    ):
        assert name in columns

    constraints = await _inspect(engine, lambda i: i.get_unique_constraints("oidc_providers"))
    assert any(entry["column_names"] == ["issuer"] for entry in constraints)


async def test_oidc_providers_status_is_constrained_to_active_or_disabled(
    engine: AsyncEngine,
) -> None:
    checks = await _inspect(engine, lambda i: i.get_check_constraints("oidc_providers"))
    assert any("status" in entry["sqltext"] for entry in checks)


async def test_oidc_login_states_fks_to_oidc_providers_and_state_is_unique(
    engine: AsyncEngine,
) -> None:
    columns = await _inspect(engine, lambda i: _columns(i, "oidc_login_states"))
    for name in (
        "provider_id",
        "state",
        "nonce",
        "code_verifier",
        "redirect_uri",
        "expires_at",
        "consumed_at",
    ):
        assert name in columns
    assert columns["consumed_at"]["nullable"] is True

    foreign_keys = await _inspect(engine, lambda i: i.get_foreign_keys("oidc_login_states"))
    matching = [fk for fk in foreign_keys if fk["constrained_columns"] == ["provider_id"]]
    assert len(matching) == 1
    assert matching[0]["referred_table"] == "oidc_providers"

    constraints = await _inspect(engine, lambda i: i.get_unique_constraints("oidc_login_states"))
    assert any(entry["column_names"] == ["state"] for entry in constraints)
