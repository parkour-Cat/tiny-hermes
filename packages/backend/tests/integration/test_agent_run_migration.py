from collections.abc import Callable
from typing import Any

from sqlalchemy import Inspector, inspect
from sqlalchemy.ext.asyncio import AsyncEngine

EXPECTED = {
    "agents",
    "agent_drafts",
    "agent_versions",
    "sessions",
    "session_messages",
    "runs",
    "run_budget_scopes",
    "run_events",
    "worker_leases",
    "idempotency_records",
}

PHASE_ONE = {
    "users",
    "auth_identities",
    "auth_sessions",
    "workspaces",
    "memberships",
    "audit_events",
}


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


async def test_phase_two_migration_creates_every_run_foundation_table(
    engine: AsyncEngine,
) -> None:
    tables = await _inspect(engine, lambda inspector: set(inspector.get_table_names()))

    assert EXPECTED <= tables
    assert PHASE_ONE <= tables


async def test_phase_two_migration_enforces_ordering_and_ownership_uniqueness(
    engine: AsyncEngine,
) -> None:
    agents = await _inspect(engine, lambda i: _unique_sets(i, "agents"))
    versions = await _inspect(engine, lambda i: _unique_sets(i, "agent_versions"))
    runs = await _inspect(engine, lambda i: _unique_sets(i, "runs"))
    events = await _inspect(engine, lambda i: _unique_sets(i, "run_events"))
    leases = await _inspect(engine, lambda i: _unique_sets(i, "worker_leases"))
    messages = await _inspect(engine, lambda i: _unique_sets(i, "session_messages"))
    records = await _inspect(engine, lambda i: _unique_sets(i, "idempotency_records"))

    assert frozenset({"workspace_id", "alias"}) in agents
    assert frozenset({"agent_id", "version_number"}) in versions
    assert frozenset({"session_id", "session_sequence"}) in runs
    assert frozenset({"run_id", "sequence"}) in events
    assert frozenset({"run_id"}) in leases
    assert frozenset({"session_id", "sequence"}) in messages
    assert (
        frozenset(
            {"workspace_id", "caller_type", "caller_id", "endpoint", "idempotency_key"}
        )
        in records
    )


async def test_phase_two_migration_pins_run_concurrency_columns(
    engine: AsyncEngine,
) -> None:
    runs = await _inspect(engine, lambda i: _columns(i, "runs"))
    budgets = await _inspect(engine, lambda i: _columns(i, "run_budget_scopes"))
    records = await _inspect(engine, lambda i: _columns(i, "idempotency_records"))

    assert runs["state_version"]["nullable"] is False
    assert runs["next_event_sequence"]["nullable"] is False
    assert runs["budget_root_run_id"]["nullable"] is False
    assert runs["retry_of_run_id"]["nullable"] is True
    assert runs["blocked_by_run_id"]["nullable"] is True
    assert runs["checkpoint_replay_safe"]["nullable"] is False
    assert budgets["max_tokens"]["nullable"] is True
    assert budgets["version"]["nullable"] is False
    assert records["expires_at"]["nullable"] is True


async def test_phase_two_migration_constrains_domain_enums(engine: AsyncEngine) -> None:
    runs = await _inspect(
        engine,
        lambda i: {item["name"] for item in i.get_check_constraints("runs")},
    )
    sessions = await _inspect(
        engine,
        lambda i: {item["name"] for item in i.get_check_constraints("sessions")},
    )

    assert "ck_runs_status" in runs
    assert "ck_runs_pause_reason" in runs
    assert "ck_runs_checkpoint_effect_status" in runs
    assert "ck_sessions_session_mode" in sessions
    assert "ck_sessions_caller_type" in sessions
