"""The Worker's workspace flows, and the guard on the limit-pause door.

Design §6.3: the rollback transaction records where the Run must go and which
sandbox must be confirmed gone; the confirmation clears those columns only in
the same transition that reaches the target. The flow tests drive the whole
loop — restore, per-round checkpoint, rollback, honest pause — with a fake
gateway sandbox over real PostgreSQL and MinIO.
"""

import hashlib
import io
import os
import socket
import tarfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tiny_hermes.runs.application.worker import (
    WorkerRuntime,
    WorkerSettings,
    WorkspaceRuntime,
)
from tiny_hermes.runs.domain.models import (
    CanonicalMessage,
    CheckpointEffectStatus,
    PauseReason,
    RunCapabilities,
    RunSignal,
    RunState,
    TextBlock,
    ToolCallBlock,
    WorkspaceCleanupTarget,
)
from tiny_hermes.runs.domain.state_machine import InvalidStateMetadata
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.infrastructure.tables import RunRow, SessionRow
from tiny_hermes.runs.ports.model import ModelRequest, ModelResponse, StopReason
from tiny_hermes.runs.ports.store import (
    ApplySignalCommand,
    ClaimRunCommand,
    RecordSliceCommand,
)
from tiny_hermes.sandbox.application.controller import RefusalReason, SandboxRefused
from tiny_hermes.sandbox.domain.command import (
    CommandResult,
    SandboxCommand,
    ScannedEntry,
)
from tiny_hermes.sandbox.domain.models import CacheState
from tiny_hermes.session_workspace.domain.models import WorkspaceQuota
from tiny_hermes.session_workspace.infrastructure.minio_store import MinioObjectStore
from tiny_hermes.session_workspace.infrastructure.tables import WorkspaceRevisionRow

from ..conftest import VALID_SPEC

PLATFORM = RunCapabilities(can_control=True, can_retry=True)
SANDBOX = uuid.uuid4()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def claimed(
    sessions: async_sessionmaker[AsyncSession], submitted_run: dict[str, Any]
) -> Any:
    del submitted_run
    async with sessions.begin() as db:
        claim = await SqlRunStore(db).claim_head(
            ClaimRunCommand(
                workspace_id=None,
                worker_id="workspace-guard",
                lease_seconds=60,
                request_id="claim-workspace-guard",
                capabilities=PLATFORM,
            )
        )
    assert claim is not None
    return claim


async def _interrupt_with_intent(
    sessions: async_sessionmaker[AsyncSession],
    claimed: Any,
    *,
    target: WorkspaceCleanupTarget | None,
) -> None:
    """The rollback transaction: intent and interruption written together."""
    async with sessions.begin() as db:
        await SqlRunStore(db).record_slice(
            RecordSliceCommand(
                workspace_id=claimed.run.workspace_id,
                run_id=claimed.run.id,
                lease_id=claimed.lease_id,
                expected_lease_version=claimed.lease_version,
                expected_state_version=claimed.run.state_version,
                signal=RunSignal.INTERRUPTED,
                pause_reason=None,
                limit_reached=False,
                checkpoint={"phase": "rolled_back"},
                checkpoint_replay_safe=True,
                checkpoint_effect_status=CheckpointEffectStatus.NONE,
                executed_ms=10,
                model_calls=0,
                tokens=0,
                request_id=f"rollback-{uuid.uuid4()}",
                capabilities=PLATFORM,
                appended=(
                    CanonicalMessage("assistant", (TextBlock(text="over limit"),)),
                ),
                workspace_cleanup_target=target,
                workspace_cleanup_sandbox_id=SANDBOX if target else None,
            )
        )


def _confirmation(claimed: Any, sandbox_id: uuid.UUID) -> ApplySignalCommand:
    return ApplySignalCommand(
        workspace_id=claimed.run.workspace_id,
        run_id=claimed.run.id,
        signal=RunSignal.LIMIT_CLEANUP_CONFIRMED,
        pause_reason=PauseReason.LIMIT,
        request_id=f"scheduler-{uuid.uuid4()}",
        capabilities=PLATFORM,
        confirmed_sandbox_id=sandbox_id,
    )


async def test_the_confirmation_requires_the_recorded_intent(
    sessions: async_sessionmaker[AsyncSession], claimed: Any
) -> None:
    await _interrupt_with_intent(sessions, claimed, target=None)

    async with sessions.begin() as db:
        with pytest.raises(InvalidStateMetadata):
            await SqlRunStore(db).apply_signal(_confirmation(claimed, SANDBOX))


async def test_the_confirmation_requires_the_recorded_sandbox(
    sessions: async_sessionmaker[AsyncSession], claimed: Any
) -> None:
    await _interrupt_with_intent(
        sessions, claimed, target=WorkspaceCleanupTarget.PAUSED_LIMIT
    )

    async with sessions.begin() as db:
        with pytest.raises(InvalidStateMetadata):
            await SqlRunStore(db).apply_signal(_confirmation(claimed, uuid.uuid4()))


async def test_the_confirmation_pauses_and_clears_the_intent_together(
    sessions: async_sessionmaker[AsyncSession], claimed: Any
) -> None:
    await _interrupt_with_intent(
        sessions, claimed, target=WorkspaceCleanupTarget.PAUSED_LIMIT
    )

    async with sessions.begin() as db:
        snapshot = await SqlRunStore(db).apply_signal(_confirmation(claimed, SANDBOX))

    assert snapshot.state is RunState.PAUSED
    assert snapshot.pause_reason is PauseReason.LIMIT
    async with sessions() as db:
        row = (
            await db.execute(select(RunRow).where(RunRow.id == claimed.run.id))
        ).scalar_one()
    assert row.workspace_cleanup_target is None
    assert row.workspace_cleanup_sandbox_id is None


async def test_a_wrong_target_is_refused_even_with_the_right_sandbox(
    sessions: async_sessionmaker[AsyncSession], claimed: Any
) -> None:
    await _interrupt_with_intent(
        sessions, claimed, target=WorkspaceCleanupTarget.FAILED_CONFLICT
    )

    async with sessions.begin() as db:
        with pytest.raises(InvalidStateMetadata):
            await SqlRunStore(db).apply_signal(_confirmation(claimed, SANDBOX))


# -- the Worker's workspace flows, end to end --------------------------------


Effect = Callable[["GatewaySandbox"], Awaitable[None] | None]


class Recording:
    """A scripted model that snapshots the sandbox's call log per request."""

    def __init__(self, sandbox: "GatewaySandbox", *answers: ModelResponse) -> None:
        self._answers = list(answers)
        self._sandbox = sandbox
        self.requests: list[ModelRequest] = []
        self.calls_at_request: list[list[str]] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        self.calls_at_request.append(list(self._sandbox.calls))
        return (
            self._answers.pop(0)
            if self._answers
            else ModelResponse(stop_reason=StopReason.COMPLETED, text="done")
        )


@dataclass
class GatewaySandbox:
    """The Controller's whole surface over an in-memory file tree."""

    files: dict[str, bytes] = field(default_factory=dict[str, bytes])
    effects: list[Effect] = field(default_factory=list["Effect"])
    calls: list[str] = field(default_factory=list[str])
    destroy_fails: bool = False
    sandbox_id: UUID = field(default_factory=uuid4)

    async def acquire(self, **_: Any) -> Any:
        self.calls.append("acquire")
        return type(
            "Acquired", (), {"sandbox_id": self.sandbox_id, "cache_state": CacheState.RESET}
        )()

    async def execute(self, *, command: SandboxCommand, **_: Any) -> CommandResult:
        if command.argv and command.argv[-1] == "probe":
            self.calls.append("probe")
            return CommandResult(exit_code=0, output="", truncated=False, timed_out=False)
        self.calls.append("execute")
        if self.effects:
            outcome = self.effects.pop(0)(self)
            if outcome is not None:
                await outcome
        return CommandResult(exit_code=0, output="ok", truncated=False, timed_out=False)

    async def freeze(self, **_: Any) -> None:
        self.calls.append("freeze")

    async def thaw(self, **_: Any) -> None:
        self.calls.append("thaw")

    async def keep(self, **_: Any) -> None:
        self.calls.append("keep")

    async def destroy(self, **_: Any) -> None:
        self.calls.append("destroy")
        if self.destroy_fails:
            raise RuntimeError("the daemon did not confirm destroy")

    async def cleanup(self, **_: Any) -> None:
        # After INTERRUPTED the WorkerLease is gone, so the real Controller
        # refuses `destroy` and this is the reclaim that can still succeed.
        self.calls.append("cleanup")
        if self.destroy_fails:
            raise RuntimeError("the daemon did not confirm destroy")

    async def workspace_scan(self, **_: Any) -> tuple[ScannedEntry, ...]:
        self.calls.append("scan")
        directories: set[str] = set()
        for path in self.files:
            parts = path.split("/")[:-1]
            for depth in range(1, len(parts) + 1):
                directories.add("/".join(parts[:depth]))
        entries = [
            ScannedEntry(path=name, entry_type="directory", mode=0o755, size=0, sha256=None)
            for name in sorted(directories)
        ]
        entries.extend(
            ScannedEntry(
                path=path,
                entry_type="file",
                mode=0o644,
                size=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
            )
            for path, body in sorted(self.files.items())
        )
        return tuple(entries)

    async def workspace_import(
        self, *, tar_stream: AsyncIterator[bytes], **_: Any
    ) -> dict[str, Any]:
        self.calls.append("import")
        received = bytearray()
        async for chunk in tar_stream:
            received.extend(chunk)
        with tarfile.open(fileobj=io.BytesIO(bytes(received)), mode="r|") as archive:
            for member in archive:
                if member.isreg():
                    body = archive.extractfile(member)
                    self.files[member.name] = body.read() if body else b""
        return {"received_bytes": len(received)}

    async def workspace_export(
        self, *, sink: Callable[[bytes], Awaitable[None]], **_: Any
    ) -> dict[str, Any]:
        self.calls.append("export")
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            root = tarfile.TarInfo("data")
            root.type = tarfile.DIRTYPE
            archive.addfile(root)
            for path, body in sorted(self.files.items()):
                info = tarfile.TarInfo(f"data/{path}")
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))
        await sink(buffer.getvalue())
        return {"total_bytes": buffer.tell()}


@pytest.fixture(scope="module")
def objects() -> MinioObjectStore:
    return MinioObjectStore(
        endpoint=os.environ.get("S3_ENDPOINT", "http://localhost:9000"),
        access_key=os.environ.get("S3_ACCESS_KEY", "tiny-hermes-local"),
        secret_key=os.environ.get("S3_SECRET_KEY", "tiny-hermes-local-password"),
        bucket=os.environ.get("S3_BUCKET", "tiny-hermes-test"),
    )


@pytest.fixture(autouse=True)
async def reachable(objects: MinioObjectStore, request: pytest.FixtureRequest) -> None:
    # ``FixtureRequest.node`` carries no annotation upstream; assert its type.
    node = cast(pytest.Item, request.node)  # pyright: ignore[reportUnknownMemberType]
    if "flow" not in node.name and "restore" not in node.name:
        return
    parsed = urlparse(os.environ.get("S3_ENDPOINT", "http://localhost:9000"))
    try:
        socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or 9000), timeout=1
        ).close()
    except OSError as unreachable:  # pragma: no cover - environment
        pytest.skip(f"no reachable MinIO: {unreachable}")
    await objects.ensure_bucket()


def _tool(command: str = "touch out", call_id: str = "c1") -> ModelResponse:
    return ModelResponse(
        stop_reason=StopReason.TOOL_CALL,
        text="",
        tool_calls=(
            ToolCallBlock(call_id=call_id, name="shell.exec", arguments={"command": command}),
        ),
    )


def _read_tool() -> ModelResponse:
    return ModelResponse(
        stop_reason=StopReason.TOOL_CALL,
        text="",
        tool_calls=(
            ToolCallBlock(call_id="r1", name="file.read", arguments={"path": "a.txt"}),
        ),
    )


def _text(said: str) -> ModelResponse:
    return ModelResponse(stop_reason=StopReason.COMPLETED, text=said)


@pytest.fixture
def tooled_agent(client: TestClient, scope: dict[str, str]) -> Callable[[list[str]], str]:
    def build(tools: list[str]) -> str:
        alias = f"ws-{uuid4().hex[:8]}"
        agent = client.post(
            "/api/v1/agents", headers=scope, json={"name": "WS", "alias": alias}
        ).json()
        draft = client.put(
            f"/api/v1/agents/{agent['id']}/draft",
            headers=scope,
            json={"expected_revision": 1, "spec": {**VALID_SPEC, "tools": tools}},
        ).json()
        client.post(
            f"/api/v1/agents/{agent['id']}/publish",
            headers=scope,
            json={"expected_revision": draft["revision"]},
        )
        return str(agent["id"])

    return build


def _submit(
    client: TestClient, scope: dict[str, str], agent_id: str, session_id: str | None = None
) -> tuple[str, str]:
    if session_id is None:
        session_id = str(
            client.post(
                "/api/v1/sessions", headers=scope, json={"agent_id": agent_id}
            ).json()["id"]
        )
    run = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": uuid4().hex},
        json={"session_id": session_id, "input": "work with files"},
    ).json()["id"]
    return str(run), session_id


async def _drive(
    engine: AsyncEngine,
    model: Any,
    sandbox: GatewaySandbox,
    objects: MinioObjectStore,
    *,
    quota: WorkspaceQuota | None = None,
    seconds: int = 30,
) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    await WorkerRuntime(
        session_factory=sessions,
        model=model,
        notifier=NullWakeUpNotifier(),
        sandbox=sandbox,
        workspace=WorkspaceRuntime(
            objects=objects,
            quota=quota or WorkspaceQuota(max_bytes=10_000_000, max_objects=10_000),
            staging_ttl_seconds=3_600,
            export_limit=100 * 1024 * 1024,
        ),
        settings=WorkerSettings(
            worker_id="worker-ws",
            lease_seconds=30,
            max_slice_seconds=seconds,
            idle_poll_seconds=1,
        ),
    ).run_once()


async def _revisions(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        found = await connection.execute(
            select(func.count()).select_from(WorkspaceRevisionRow.__table__)
        )
        return int(found.scalar_one())


async def _run_row(engine: AsyncEngine, run_id: str) -> Any:
    async with engine.connect() as connection:
        return (
            await connection.execute(text(
                "SELECT status, pause_reason, workspace_cleanup_target, "
                "workspace_cleanup_sandbox_id FROM runs WHERE id = :run"
            ), {"run": run_id})
        ).one()


async def _events_of(engine: AsyncEngine, run_id: str) -> list[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT event_type FROM run_events WHERE run_id = :run ORDER BY sequence"),
            {"run": run_id},
        )
        return [str(row.event_type) for row in rows.all()]


def _write_effect(path: str, body: bytes) -> Effect:
    def apply(sandbox: GatewaySandbox) -> None:
        sandbox.files[path] = body

    return apply


async def test_write_round_flow_checkpoints_before_the_next_model_call(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    objects: MinioObjectStore,
    tooled_agent: Callable[[list[str]], str],
) -> None:
    agent = tooled_agent(["shell.exec"])
    run, _ = _submit(client, scope, agent)
    sandbox = GatewaySandbox(effects=[_write_effect("out.txt", b"made by round 1")])
    model = Recording(sandbox, _tool(), _text("wrote it"))

    await _drive(engine, model, sandbox, objects)

    assert (await _run_row(engine, run)).status == "completed"
    assert await _revisions(engine) == 1
    # The second model call happened only after the frozen scan and commit.
    second_round = model.calls_at_request[1]
    after_execute = second_round[second_round.index("execute") :]
    assert "freeze" in after_execute and "export" in after_execute
    assert after_execute[-1] == "thaw", "the model speaks to a running container"


async def test_fresh_sandbox_restore_flow_runs_before_the_first_model_call(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    objects: MinioObjectStore,
    tooled_agent: Callable[[list[str]], str],
) -> None:
    agent = tooled_agent(["shell.exec"])
    first_run, session = _submit(client, scope, agent)
    seeded = GatewaySandbox(effects=[_write_effect("a.txt", b"persisted")])
    await _drive(engine, Recording(seeded, _tool(), _text("saved")), seeded, objects)
    assert (await _run_row(engine, first_run)).status == "completed"

    second_run, _ = _submit(client, scope, agent, session_id=session)
    fresh = GatewaySandbox()
    model = Recording(fresh, _text("looked around"))
    await _drive(engine, model, fresh, objects)

    assert (await _run_row(engine, second_run)).status == "completed"
    assert fresh.files == {"a.txt": b"persisted"}
    before_model = model.calls_at_request[0]
    assert before_model[:2] == ["acquire", "freeze"]
    assert "import" in before_model and "scan" in before_model
    assert before_model.index("import") < before_model.index("thaw")


async def test_over_quota_flow_rolls_back_cleans_up_then_pauses_limit(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    objects: MinioObjectStore,
    tooled_agent: Callable[[list[str]], str],
) -> None:
    agent = tooled_agent(["shell.exec"])
    run, _ = _submit(client, scope, agent)
    sandbox = GatewaySandbox(effects=[_write_effect("huge.bin", b"x" * 500)])
    model = Recording(sandbox, _tool())

    await _drive(
        engine, model, sandbox, objects, quota=WorkspaceQuota(max_bytes=100, max_objects=10)
    )

    row = await _run_row(engine, run)
    assert row.status == "paused"
    assert row.pause_reason == "limit"
    assert row.workspace_cleanup_target is None, "cleared in the same transition"
    assert "cleanup" in sandbox.calls
    assert "destroy" not in sandbox.calls
    assert "workspace_limit_exceeded" in await _events_of(engine, run)
    assert await _revisions(engine) == 0, "the over-limit step never becomes a revision"
    assert len(model.requests) == 1, "no model call may follow an unhandled rollback"


async def test_over_quota_cleanup_confirms_after_the_lease_is_released(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    objects: MinioObjectStore,
    tooled_agent: Callable[[list[str]], str],
) -> None:
    """The real Controller refuses Worker `destroy` once INTERRUPTED released the lease.

    CI's quota drill hung here: the rollback was recorded, `destroy` came back
    `lease_invalid`, and the Run sat in `interrupted` until the Scheduler's
    next cycle. The Worker must use the no-lease `cleanup` action itself so
    the pause does not wait on that sweep.
    """

    class LeaseReleasedSandbox(GatewaySandbox):
        async def destroy(self, **_: Any) -> None:
            self.calls.append("destroy")
            raise SandboxRefused(RefusalReason.LEASE_INVALID)

    agent = tooled_agent(["shell.exec"])
    run, _ = _submit(client, scope, agent)
    sandbox = LeaseReleasedSandbox(effects=[_write_effect("huge.bin", b"x" * 500)])
    await _drive(
        engine,
        Recording(sandbox, _tool()),
        sandbox,
        objects,
        quota=WorkspaceQuota(max_bytes=100, max_objects=10),
    )

    row = await _run_row(engine, run)
    assert row.status == "paused"
    assert row.pause_reason == "limit"
    assert "cleanup" in sandbox.calls
    assert "destroy" not in sandbox.calls


async def test_unconfirmed_destroy_flow_stays_interrupted_not_paused(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    objects: MinioObjectStore,
    tooled_agent: Callable[[list[str]], str],
) -> None:
    agent = tooled_agent(["shell.exec"])
    run, _ = _submit(client, scope, agent)
    sandbox = GatewaySandbox(
        effects=[_write_effect("huge.bin", b"x" * 500)], destroy_fails=True
    )

    await _drive(
        engine,
        Recording(sandbox, _tool()),
        sandbox,
        objects,
        quota=WorkspaceQuota(max_bytes=100, max_objects=10),
    )

    row = await _run_row(engine, run)
    assert row.status == "interrupted", "a safe pause may not be claimed on a maybe"
    assert row.workspace_cleanup_target == "paused_limit"
    assert str(row.workspace_cleanup_sandbox_id) == str(sandbox.sandbox_id)


async def test_conflict_flow_becomes_failed_workspace_conflict_after_cleanup(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    objects: MinioObjectStore,
    tooled_agent: Callable[[list[str]], str],
) -> None:
    agent = tooled_agent(["shell.exec"])
    run, session = _submit(client, scope, agent)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def concurrent_commit(sandbox: GatewaySandbox) -> None:
        """Somebody else advances the Session pointer mid-round."""
        sandbox.files["mine.txt"] = b"contender"
        foreign = uuid.uuid4()
        async with sessions.begin() as db:
            row = (
                await db.execute(select(SessionRow).where(SessionRow.id == UUID(session)))
            ).scalar_one()
            db.add(
                WorkspaceRevisionRow(
                    id=foreign,
                    workspace_id=row.workspace_id,
                    session_id=row.id,
                    parent_revision_id=None,
                    manifest_schema_version=1,
                    manifest_object_key=f"manifests/{foreign}.json",
                    manifest_sha256="0" * 64,
                    total_bytes=0,
                    object_count=0,
                    created_by_run_id=UUID(run),
                )
            )
            row.workspace_revision_id = foreign

    sandbox = GatewaySandbox(effects=[concurrent_commit])

    await _drive(engine, Recording(sandbox, _tool()), sandbox, objects)

    row = await _run_row(engine, run)
    assert row.status == "failed"
    assert row.workspace_cleanup_target is None
    assert "cleanup" in sandbox.calls
    assert "workspace_conflict" in await _events_of(engine, run)


async def test_unchanged_write_round_flow_still_records_its_turns(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    objects: MinioObjectStore,
    tooled_agent: Callable[[list[str]], str],
) -> None:
    """A shell round that changed nothing commits nothing — but the transcript
    must still gain the round's turns, or the next round rebuilds a different
    conversation and the model repeats the command forever."""
    agent = tooled_agent(["shell.exec"])
    run, _ = _submit(client, scope, agent)
    # Round one writes and commits; round two touches nothing.
    sandbox = GatewaySandbox(effects=[_write_effect("stable.txt", b"unmoved")])

    model = Recording(
        sandbox, _tool(call_id="c1"), _tool(call_id="c2"), _text("nothing new")
    )
    await _drive(engine, model, sandbox, objects)

    assert (await _run_row(engine, run)).status == "completed"
    assert await _revisions(engine) == 1, "the unchanged round must not commit again"
    assert len(model.requests) == 3, "an unrecorded round would loop the model"
    async with engine.connect() as connection:
        roles = [
            str(row.role)
            for row in (
                await connection.execute(
                    text(
                        "SELECT m.role FROM session_messages m "
                        "JOIN runs r ON r.session_id = m.session_id "
                        "WHERE r.id = :run ORDER BY m.sequence"
                    ),
                    {"run": run},
                )
            ).all()
        ]
    assert roles == ["user", "assistant", "tool", "assistant", "tool", "assistant"]


async def test_read_only_flow_creates_no_revision_and_never_freezes(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    objects: MinioObjectStore,
    tooled_agent: Callable[[list[str]], str],
) -> None:
    agent = tooled_agent(["file.read", "shell.exec"])
    run, _ = _submit(client, scope, agent)
    sandbox = GatewaySandbox(files={"a.txt": b"already here... wait"})
    sandbox.files.clear()  # empty workspace; the read will simply fail inside

    await _drive(engine, Recording(sandbox, _read_tool(), _text("read it")), sandbox, objects)

    assert (await _run_row(engine, run)).status == "completed"
    assert await _revisions(engine) == 0
    assert "export" not in sandbox.calls, "a round that looked has nothing to commit"
    assert "probe" in sandbox.calls, "file tools exist only after the openat2 probe"


async def test_slice_boundary_flow_checkpoints_then_keeps_the_frozen_instance(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    objects: MinioObjectStore,
    tooled_agent: Callable[[list[str]], str],
) -> None:
    agent = tooled_agent(["shell.exec"])
    run, _ = _submit(client, scope, agent)
    sandbox = GatewaySandbox(effects=[_write_effect("kept.txt", b"survives the slice")])

    await _drive(engine, Recording(sandbox, _tool()), sandbox, objects, seconds=0)

    assert (await _run_row(engine, run)).status == "queued"
    assert await _revisions(engine) == 1
    assert "keep" in sandbox.calls
    assert "destroy" not in sandbox.calls
