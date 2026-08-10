import asyncio
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.service import RunCoordination
from tiny_hermes.runs.domain.models import RunCapabilities, RunState
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.store import ClaimedRun, ClaimRunCommand, RepairResult

FULL = RunCapabilities(can_control=True, can_retry=True)


def _submit(
    client: TestClient, scope: dict[str, str], session_id: str, key: str
) -> dict[str, object]:
    response = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": key},
        json={"session_id": session_id, "input": f"message {key}"},
    )
    assert response.status_code == 201
    return dict(response.json())


async def _claim(
    engine: AsyncEngine, workspace_id: str, worker_id: str, barrier: asyncio.Barrier
) -> ClaimedRun | None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        await barrier.wait()
        return await RunCoordination(SqlRunStore(session)).claim_head_run(
            ClaimRunCommand(
                workspace_id=UUID(workspace_id),
                worker_id=worker_id,
                lease_seconds=30,
                request_id=f"claim-{worker_id}",
                capabilities=FULL,
            )
        )


async def _repair(engine: AsyncEngine, session_id: str) -> RepairResult:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        return await RunCoordination(SqlRunStore(session)).repair_session_head(
            UUID(session_id), "repair-1"
        )


async def test_two_claimers_cannot_obtain_the_same_run(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    run = _submit(client, scope, session_id, "key-1")
    run_id = UUID(str(run["id"]))
    barrier = asyncio.Barrier(2)

    results = await asyncio.gather(
        _claim(engine, scope["X-Workspace-Id"], "worker-a", barrier),
        _claim(engine, scope["X-Workspace-Id"], "worker-b", barrier),
        return_exceptions=True,
    )
    claimed = [value for value in results if isinstance(value, ClaimedRun)]
    assert [value for value in results if isinstance(value, BaseException)] == []
    assert len(claimed) == 1
    assert claimed[0].run.id == run_id
    assert claimed[0].run.state is RunState.RUNNING

    async with engine.connect() as connection:
        leases = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM worker_leases "
                    "WHERE run_id = :run AND released_at IS NULL"
                ),
                {"run": run_id},
            )
        ).scalar_one()
        started = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM run_events "
                    "WHERE run_id = :run AND event_type = 'run_lease_acquired'"
                ),
                {"run": run_id},
            )
        ).scalar_one()
        state = (
            await connection.execute(
                text("SELECT status, state_version FROM runs WHERE id = :run"),
                {"run": run_id},
            )
        ).one()

    assert leases == 1
    assert started == 1
    assert state[0] == "running"
    assert state[1] == 2


async def test_only_the_head_run_can_be_claimed(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    first = _submit(client, scope, session_id, "key-1")
    second = _submit(client, scope, session_id, "key-2")
    barrier = asyncio.Barrier(1)

    claimed = await _claim(engine, scope["X-Workspace-Id"], "worker-a", barrier)
    assert claimed is not None
    assert claimed.run.id == UUID(str(first["id"]))

    again = await _claim(engine, scope["X-Workspace-Id"], "worker-b", asyncio.Barrier(1))
    assert again is None

    reloaded = client.get(f"/api/v1/runs/{second['id']}", headers=scope).json()
    assert reloaded["status"] == "queued"


async def test_repair_moves_the_head_off_a_terminal_run(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    first = _submit(client, scope, session_id, "key-1")
    second = _submit(client, scope, session_id, "key-2")

    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE runs SET status = 'completed', finished_at = now() WHERE id = :id"
            ),
            {"id": UUID(str(first["id"]))},
        )

    result = await _repair(engine, session_id)

    assert result.changed is True
    assert result.head_run_id == UUID(str(second["id"]))
    reloaded = client.get(f"/api/v1/runs/{second['id']}", headers=scope).json()
    assert reloaded["blocked_by_run_id"] is None
    assert reloaded["queue"] == {"position": 1, "status": "head"}
    await _assert_repair_records(engine, UUID(str(second["id"])), audits=1, events=1)


async def test_repair_restores_a_missing_head(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    first = _submit(client, scope, session_id, "key-1")

    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE sessions SET head_run_id = NULL WHERE id = :id"),
            {"id": UUID(session_id)},
        )

    result = await _repair(engine, session_id)

    assert result.changed is True
    assert result.head_run_id == UUID(str(first["id"]))


async def test_repair_pulls_the_head_back_to_the_smallest_sequence(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    first = _submit(client, scope, session_id, "key-1")
    second = _submit(client, scope, session_id, "key-2")

    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE sessions SET head_run_id = :head WHERE id = :id"),
            {"head": UUID(str(second["id"])), "id": UUID(session_id)},
        )

    result = await _repair(engine, session_id)

    assert result.changed is True
    assert result.head_run_id == UUID(str(first["id"]))


async def test_repair_corrects_wrong_pending_blockers(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    first = _submit(client, scope, session_id, "key-1")
    second = _submit(client, scope, session_id, "key-2")

    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE runs SET blocked_by_run_id = NULL WHERE id = :id"),
            {"id": UUID(str(second["id"]))},
        )

    result = await _repair(engine, session_id)

    assert result.changed is True
    assert result.head_run_id == UUID(str(first["id"]))
    reloaded = client.get(f"/api/v1/runs/{second['id']}", headers=scope).json()
    assert reloaded["blocked_by_run_id"] == first["id"]


async def test_repair_is_idempotent_and_silent_when_nothing_changes(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    _submit(client, scope, session_id, "key-1")
    _submit(client, scope, session_id, "key-2")

    first = await _repair(engine, session_id)
    second = await _repair(engine, session_id)

    assert first.changed is False
    assert second.changed is False
    async with engine.connect() as connection:
        audits = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE action = 'session.head_repaired'"
                )
            )
        ).scalar_one()
        events = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM run_events "
                    "WHERE event_type = 'session_head_repaired'"
                )
            )
        ).scalar_one()
    assert audits == 0
    assert events == 0


async def test_repair_of_an_all_terminal_session_writes_no_run_event(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    run = _submit(client, scope, session_id, "key-1")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE runs SET status = 'completed', finished_at = now() WHERE id = :id"
            ),
            {"id": UUID(str(run["id"]))},
        )

    result = await _repair(engine, session_id)

    assert result.changed is True
    assert result.head_run_id is None
    async with engine.connect() as connection:
        audits = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE action = 'session.head_repaired'"
                )
            )
        ).scalar_one()
        events = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM run_events "
                    "WHERE event_type = 'session_head_repaired'"
                )
            )
        ).scalar_one()
    assert audits == 1
    assert events == 0


async def _assert_repair_records(
    engine: AsyncEngine, head_run_id: UUID, audits: int, events: int
) -> None:
    async with engine.connect() as connection:
        audit_count = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE action = 'session.head_repaired'"
                )
            )
        ).scalar_one()
        event_count = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM run_events "
                    "WHERE run_id = :run AND event_type = 'session_head_repaired'"
                ),
                {"run": head_run_id},
            )
        ).scalar_one()
    assert audit_count == audits
    assert event_count == events
