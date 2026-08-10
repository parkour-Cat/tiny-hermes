from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Inspector, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

SEED_EVENT = (
    "INSERT INTO run_events "
    "(id, run_id, workspace_id, sequence, event_type, payload, occurred_at) "
    "SELECT :id, id, workspace_id, next_event_sequence + 100, :event_type, "
    "  '{}'::json, now() "
    "FROM runs WHERE id = :run_id"
)


async def _inspect[T](engine: AsyncEngine, read: Callable[[Inspector], T]) -> T:
    async with engine.connect() as connection:
        return await connection.run_sync(lambda sync: read(inspect(sync)))


def _columns(inspector: Inspector, table: str) -> dict[str, Any]:
    return {column["name"]: column for column in inspector.get_columns(table)}


async def test_execution_columns_exist_with_safe_defaults(engine: AsyncEngine) -> None:
    runs = await _inspect(engine, lambda i: _columns(i, "runs"))
    constraints = await _inspect(
        engine, lambda i: {item["name"] for item in i.get_check_constraints("runs")}
    )

    assert runs["recovery_attempts"]["nullable"] is False
    assert "0" in str(runs["recovery_attempts"]["default"])
    assert runs["last_heartbeat_at"]["nullable"] is True
    assert "ck_runs_recovery_attempts" in constraints


async def test_a_new_run_starts_unrecovered_and_without_a_heartbeat(
    submitted_run: dict[str, Any], engine: AsyncEngine
) -> None:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT recovery_attempts, last_heartbeat_at FROM runs WHERE id = :id"
                ),
                {"id": UUID(str(submitted_run["id"]))},
            )
        ).one()

    assert row[0] == 0
    assert row[1] is None


async def test_the_event_type_constraint_accepts_the_safety_valve_event(
    submitted_run: dict[str, Any], engine: AsyncEngine
) -> None:
    async with engine.connect() as connection:
        await connection.execute(
            text(SEED_EVENT),
            {
                "id": uuid4(),
                "event_type": "run_limit_reached",
                "run_id": UUID(str(submitted_run["id"])),
            },
        )
        await connection.rollback()


async def test_the_event_type_constraint_still_rejects_an_invented_name(
    submitted_run: dict[str, Any], engine: AsyncEngine
) -> None:
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(
                text(SEED_EVENT),
                {
                    "id": uuid4(),
                    "event_type": "run_not_a_real_event",
                    "run_id": UUID(str(submitted_run["id"])),
                },
            )
