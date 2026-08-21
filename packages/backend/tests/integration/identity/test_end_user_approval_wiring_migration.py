"""What migration 0033 actually put in the database.

Same technique as `test_end_user_run_wiring_migration.py`: inspect the schema
`alembic upgrade head` produced, not the ORM model it was written from.

`approvals.requested_by` was FK'd to `users.id` since 0020, correct for as
long as the only row that could ever set it was a workspace member's own id.
0032 gave `USER_CONFIRMATION` a producer (`SqlApprovalGate._subject` returns
`run.end_user_id`), and that id now sometimes comes from `end_users`, a table
with no relationship to `users` at all — the same one-column-two-foreign-keys
shape 0032 already solved for `runs.end_user_id`, discovered here because
nothing exercised the `user_confirmation` producer against a real database
until this task tried to build its consumer.
"""

from collections.abc import Callable

from sqlalchemy import Inspector, inspect
from sqlalchemy.ext.asyncio import AsyncEngine


async def _inspect[T](engine: AsyncEngine, read: Callable[[Inspector], T]) -> T:
    async with engine.connect() as connection:
        return await connection.run_sync(lambda sync: read(inspect(sync)))


async def test_approvals_requested_by_no_longer_fks_to_users(engine: AsyncEngine) -> None:
    foreign_keys = await _inspect(engine, lambda i: i.get_foreign_keys("approvals"))
    named = {fk["name"] for fk in foreign_keys}
    assert "fk_approvals_requested_by" not in named
    assert not any(fk["constrained_columns"] == ["requested_by"] for fk in foreign_keys)
