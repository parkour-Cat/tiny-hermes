"""What migration 0032 actually put in the database.

Same technique as `test_end_user_identity_migration.py` and
`test_end_user_session_migration.py`: inspect the schema `alembic upgrade
head` produced, not the ORM model it was written from. Three unrelated
changes, one migration — see its own docstring for why — and one test each
here.
"""

from collections.abc import Callable
from typing import Any, cast

from sqlalchemy import Inspector, inspect
from sqlalchemy.ext.asyncio import AsyncEngine


async def _inspect[T](engine: AsyncEngine, read: Callable[[Inspector], T]) -> T:
    async with engine.connect() as connection:
        return await connection.run_sync(lambda sync: read(inspect(sync)))


def _columns(inspector: Inspector, table: str) -> dict[str, Any]:
    return {column["name"]: column for column in inspector.get_columns(table)}


async def test_end_user_sessions_gained_the_agents_column(engine: AsyncEngine) -> None:
    columns = await _inspect(engine, lambda i: _columns(i, "end_user_sessions"))
    assert columns["agents"]["nullable"] is False


async def test_memories_subject_type_check_now_names_end_user(engine: AsyncEngine) -> None:
    checks = await _inspect(
        engine, lambda i: cast(list[dict[str, Any]], i.get_check_constraints("memories"))
    )
    sqltext = next(
        item["sqltext"] for item in checks if item["name"] == "ck_memories_subject_type"
    )
    assert "end_user" in sqltext


async def test_runs_end_user_id_no_longer_fks_to_users(engine: AsyncEngine) -> None:
    """§5: the column now holds an `end_users.id` for a `caller_type=end_user`
    Run, which a FK fixed to `users.id` could never accept. `runs/infra
    structure/tables.py`'s own comment explains why no FK replaces it —
    `sessions.caller_id`'s polymorphic-subject precedent, checked by
    `CallerType` in code rather than by the schema.
    """
    foreign_keys = await _inspect(engine, lambda i: i.get_foreign_keys("runs"))
    named = {fk["name"] for fk in foreign_keys}
    assert "fk_runs_end_user" not in named
    assert not any(fk["constrained_columns"] == ["end_user_id"] for fk in foreign_keys)
