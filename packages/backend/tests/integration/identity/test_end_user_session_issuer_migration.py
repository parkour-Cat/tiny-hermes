"""What migration 0034 actually put in the database.

Task-7 review finding 3: `SqlEndUserStore.active_allowed_origins` unioned
`allowed_origins` across every *active* `channel_issuers` row in a
workspace, when the design says the check is against *that issuer's*
registered origins alone. The reason given was that `end_user_sessions`
carried no record of which issuer minted it — this migration is the fix:
a nullable FK to the issuer a session's credential was exchanged through,
so a write can be checked against that one issuer's origins and nothing
wider.

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


async def test_end_user_sessions_gained_a_nullable_channel_issuer_id(
    engine: AsyncEngine,
) -> None:
    columns = await _inspect(engine, lambda i: _columns(i, "end_user_sessions"))
    assert "channel_issuer_id" in columns
    # Nullable on purpose: a session minted before this column existed has
    # no issuer to record retroactively. The application layer treats that
    # absence as "this session's origin cannot be verified" and refuses its
    # cross-origin writes rather than falling back to the old union — see
    # `EndUserIdentityService.allowed_origins_for_issuer`.
    assert columns["channel_issuer_id"]["nullable"] is True


async def test_end_user_sessions_channel_issuer_id_fks_to_channel_issuers(
    engine: AsyncEngine,
) -> None:
    foreign_keys = await _inspect(engine, lambda i: i.get_foreign_keys("end_user_sessions"))
    matching = [
        fk for fk in foreign_keys if fk["constrained_columns"] == ["channel_issuer_id"]
    ]
    assert len(matching) == 1
    assert matching[0]["referred_table"] == "channel_issuers"
