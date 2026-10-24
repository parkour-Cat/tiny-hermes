"""The Worker's loop, once a round can ask for a tool.

The sandbox here is a stand-in rather than a real container: what is under test
is when the Worker acquires, executes, freezes and destroys, and in what order —
not whether Docker works, which `test_controller.py` and `test_shell_exec.py`
prove against a real daemon. A container per test would make these slow enough
that the ordering rules stopped getting written.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.domain.models import (
    CacheStateHint,
    ToolCallBlock,
)
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.ports.model import ModelRequest, ModelResponse, StopReason
from tiny_hermes.sandbox.domain.command import CommandResult, SandboxCommand
from tiny_hermes.sandbox.domain.models import CacheState

from ..conftest import VALID_SPEC
from ..support import metric


class Recording:
    """A model that answers from a script and records what it was asked."""

    def __init__(self, *answers: ModelResponse) -> None:
        self._answers = list(answers)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self._answers.pop(0) if self._answers else _text("done")


@dataclass
class StandInSandbox:
    """The Controller's surface, without Docker."""

    cache_state: CacheState = CacheState.RESET
    freeze_fails: bool = False
    destroy_fails: bool = False
    output: str = "a.txt\n"
    calls: list[str] = field(default_factory=list[str])
    sandbox_id: UUID = field(default_factory=uuid4)
    #: The `memory_mb` each acquire asked for (§21.3, v2.22).
    sizes: list[int | None] = field(default_factory=list[int | None])

    async def acquire(
        self,
        *,
        run_id: UUID,
        lease_id: UUID,
        workspace_id: UUID,
        profile: str,
        session_id: UUID | None = None,
        memory_mb: int | None = None,
    ) -> Any:
        del run_id, lease_id, workspace_id, profile, session_id
        self.calls.append("acquire")
        self.sizes.append(memory_mb)
        return _Acquired(self.sandbox_id, self.cache_state)

    async def execute(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID, command: SandboxCommand
    ) -> CommandResult:
        del run_id, lease_id, sandbox_id, command
        self.calls.append("execute")
        return CommandResult(exit_code=0, output=self.output, truncated=False, timed_out=False)

    async def freeze(self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID) -> None:
        del run_id, lease_id, sandbox_id
        self.calls.append("freeze")
        if self.freeze_fails:
            raise RuntimeError("the daemon did not answer")

    async def keep(self, *, run_id: UUID, sandbox_id: UUID, until: datetime) -> None:
        del run_id, sandbox_id, until
        self.calls.append("keep")

    async def destroy(self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID) -> None:
        del run_id, lease_id, sandbox_id
        self.calls.append("destroy")
        if self.destroy_fails:
            raise RuntimeError("the daemon did not answer")

    async def cleanup(self, *, run_id: UUID, sandbox_id: UUID) -> None:
        del run_id, sandbox_id
        raise AssertionError("a slice that still holds a lease destroys, it does not clean up")


@dataclass(frozen=True)
class _Acquired:
    sandbox_id: UUID
    cache_state: CacheState


def _text(said: str) -> ModelResponse:
    return ModelResponse(stop_reason=StopReason.COMPLETED, text=said)


def _tool(command: str = "ls", said: str = "") -> ModelResponse:
    return ModelResponse(
        stop_reason=StopReason.TOOL_CALL,
        text=said,
        tool_calls=(
            ToolCallBlock(call_id="c1", name="shell.exec", arguments={"command": command}),
        ),
    )


@pytest.fixture
def agent_with_tools(client: TestClient, scope: dict[str, str]) -> Any:
    def build(tools: list[str], **extra: Any) -> str:
        alias = f"tooled-{uuid4().hex[:8]}"
        agent = client.post(
            "/api/v1/agents", headers=scope, json={"name": "Tooled", "alias": alias}
        ).json()
        draft = client.put(
            f"/api/v1/agents/{agent['id']}/draft",
            headers=scope,
            json={"expected_revision": 1, "spec": {**VALID_SPEC, "tools": tools, **extra}},
        ).json()
        client.post(
            f"/api/v1/agents/{agent['id']}/publish",
            headers=scope,
            json={"expected_revision": draft["revision"]},
        )
        return str(agent["id"])

    return build


async def drive(
    engine: AsyncEngine, model: Any, sandbox: Any | None, seconds: int = 30
) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    await WorkerRuntime(
        session_factory=sessions,
        model=model,
        notifier=NullWakeUpNotifier(),
        sandbox=sandbox,
        settings=WorkerSettings(
            worker_id="worker-tools",
            lease_seconds=30,
            max_slice_seconds=seconds,
            idle_poll_seconds=1,
        ),
    ).run_once()


def submit(client: TestClient, scope: dict[str, str], agent_id: str, said: str) -> str:
    session = client.post(
        "/api/v1/sessions", headers=scope, json={"agent_id": agent_id}
    ).json()
    return str(
        client.post(
            "/api/v1/runs",
            headers={**scope, "Idempotency-Key": uuid4().hex},
            json={"session_id": session["id"], "input": said},
        ).json()["id"]
    )


async def transcript(engine: AsyncEngine, run_id: str) -> list[tuple[str, str]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT m.role, m.content FROM session_messages m "
                "JOIN runs r ON r.session_id = m.session_id "
                "WHERE r.id = :run ORDER BY m.sequence"
            ),
            {"run": run_id},
        )
        return [(row.role, str(row.content)) for row in rows.all()]


async def events(engine: AsyncEngine, run_id: str) -> list[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT event_type FROM run_events WHERE run_id = :run ORDER BY sequence"),
            {"run": run_id},
        )
        return [str(row.event_type) for row in rows.all()]


# -- when a sandbox is created at all --------------------------------------


async def test_a_run_without_tools_never_touches_the_sandbox(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    """§11.4: 没有工具绑定的 Run 不为此提前创建沙箱.

    Most Runs bind no tools, and starting a container for each of them would
    cost a second and a gigabyte for nothing.
    """
    agent = agent_with_tools([])
    run = submit(client, scope, agent, "just answer")
    sandbox = StandInSandbox()

    await drive(engine, Recording(_text("the answer")), sandbox)

    assert sandbox.calls == []
    assert client.get(f"/api/v1/runs/{run}", headers=scope).json()["status"] == "completed"


async def test_a_reply_in_words_from_a_tooled_agent_never_touches_the_sandbox(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    """§16, v2.15: the sandbox is taken by the round that needs one.

    An Agent bound to `shell.exec` is mostly talked to, not run: a greeting
    that took a container would hold a share of §21.3's memory budget that a
    Run with a command to run is then refused.
    """
    agent = agent_with_tools(["shell.exec"])
    run = submit(client, scope, agent, "hello")
    sandbox = StandInSandbox()

    await drive(engine, Recording(_text("hi there")), sandbox)

    assert sandbox.calls == []
    assert client.get(f"/api/v1/runs/{run}", headers=scope).json()["status"] == "completed"


async def test_a_round_of_platform_tools_never_touches_the_sandbox(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    """Platform tools are answered by the Worker, so a round of them has
    nothing to put in a container either."""
    agent = agent_with_tools(["shell.exec", "todo.write"])
    run = submit(client, scope, agent, "plan it")
    keep_a_list = ModelResponse(
        stop_reason=StopReason.TOOL_CALL,
        text="",
        tool_calls=(
            ToolCallBlock(
                call_id="t1",
                name="todo.write",
                arguments={"items": [{"id": "1", "content": "look", "status": "pending"}]},
            ),
        ),
    )
    sandbox = StandInSandbox()

    await drive(engine, Recording(keep_a_list, _text("planned")), sandbox)

    assert sandbox.calls == []
    assert client.get(f"/api/v1/runs/{run}", headers=scope).json()["status"] == "completed"


async def test_the_sandbox_is_acquired_after_the_model_asks_for_a_command(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    agent = agent_with_tools(["shell.exec"])
    submit(client, scope, agent, "list the files")
    asked: list[int] = []
    model = Recording(_tool(), _text("done"))

    class Watching(StandInSandbox):
        async def acquire(self, **kwargs: Any) -> Any:
            asked.append(len(model.requests))
            return await super().acquire(**kwargs)

    await drive(engine, model, Watching())

    assert asked == [1], "acquired once, after the first model call and before its tool ran"


async def test_a_worker_with_no_sandbox_fails_a_tool_run_rather_than_ignoring_it(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    """§11.5's `sandbox_start_failed`, and product design §16's rule that a
    sandbox failure never falls back to the host."""
    agent = agent_with_tools(["shell.exec"])
    run = submit(client, scope, agent, "list the files")

    await drive(engine, Recording(_tool(), _text("done")), None)

    snapshot = client.get(f"/api/v1/runs/{run}", headers=scope).json()
    assert snapshot["status"] == "failed"
    assert snapshot["failure_reason"] == "sandbox_not_configured"
    # v2.15: the call that asked for the tool was made, so it is charged.
    assert snapshot["budget"]["consumed_model_calls"] == 1
    roles = [role for role, _ in await transcript(engine, run)]
    assert roles == ["user"], "a tool call nobody could run is not kept"


# -- the tool round --------------------------------------------------------


async def test_a_tool_call_runs_and_the_result_returns_to_the_model(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    agent = agent_with_tools(["shell.exec"])
    run = submit(client, scope, agent, "list the files")
    sandbox = StandInSandbox(output="a.txt\nb.txt\n")
    model = Recording(_tool("ls", said="Looking."), _text("There are two files."))

    await drive(engine, model, sandbox)

    assert sandbox.calls == ["acquire", "execute", "destroy"]
    # The second round was given the first round's call and its result.
    second = model.requests[1].messages
    assert any(
        isinstance(block, ToolCallBlock) for message in second for block in message.blocks
    )
    assert any("a.txt" in message.text or "a.txt" in str(message.document()) for message in second)
    assert client.get(f"/api/v1/runs/{run}", headers=scope).json()["status"] == "completed"


async def test_both_turns_land_in_the_transcript(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    """The assistant's call and the tool's answer, in the slice transaction.

    A transcript missing the result would make the next Run in the Session
    believe the Agent asked a question nobody answered.
    """
    agent = agent_with_tools(["shell.exec"])
    run = submit(client, scope, agent, "list the files")
    await drive(engine, Recording(_tool(), _text("two files")), StandInSandbox())

    roles = [role for role, _ in await transcript(engine, run)]
    assert roles == ["user", "assistant", "tool", "assistant"]


@pytest.mark.parametrize("output", ["", "file.txt"])
async def test_empty_file_tool_results_are_counted_in_run_and_workspace_usage(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any,
    output: str,
) -> None:
    agent = agent_with_tools(["file.list"])
    run = submit(client, scope, agent, "list two directories")
    sandbox = StandInSandbox(output=output)
    model = Recording(ModelResponse(
        stop_reason=StopReason.TOOL_CALL,
        text="",
        tool_calls=tuple(
            ToolCallBlock(call_id=f"c{index}", name="file.list", arguments={"path": "."})
            for index in range(2)
        ),
    ), _text("done"))

    await drive(engine, model, sandbox)

    assert sandbox.calls.count("execute") == 2
    snapshot = client.get(f"/api/v1/runs/{run}", headers=scope).json()
    assert snapshot["budget"]["consumed_tool_calls"] == 2
    usage = client.get("/api/v1/usage", headers=scope).json()
    assert usage["total_tool_calls"] == 2


async def test_a_multi_call_round_stops_dispatching_at_the_tool_limit(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    agent = agent_with_tools(["file.list"])
    run = submit(client, scope, agent, "list directories")
    async with engine.begin() as db:
        await db.execute(
            text("UPDATE run_budget_scopes SET max_tool_calls=1 WHERE root_run_id=:id"),
            {"id": run},
        )
    sandbox = StandInSandbox()
    response = ModelResponse(stop_reason=StopReason.TOOL_CALL, text="", tool_calls=tuple(
        ToolCallBlock(call_id=f"c{index}", name="file.list", arguments={"path": "."})
        for index in range(3)
    ))

    await drive(engine, Recording(response), sandbox)

    assert sandbox.calls.count("execute") == 1
    snapshot = client.get(f"/api/v1/runs/{run}", headers=scope).json()
    assert snapshot["budget"]["consumed_tool_calls"] == 1
    assert snapshot["status"] == "paused"
    assert snapshot["pause_reason"] == "limit"


async def test_two_tool_rounds_reuse_one_container(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    """The roadmap's phase-three check: 同一时间片多工具步骤不重建容器."""
    agent = agent_with_tools(["shell.exec"])
    submit(client, scope, agent, "do two things")
    sandbox = StandInSandbox()

    await drive(engine, Recording(_tool("ls"), _tool("pwd"), _text("done")), sandbox)

    assert sandbox.calls.count("acquire") == 1
    assert sandbox.calls.count("execute") == 2


# -- what the Agent is told about its cache --------------------------------


async def test_a_reset_cache_produces_an_event_and_a_protected_hint(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    """§11.3. The event is the half a person reads; the hint is the model's.

    Without the hint an Agent carries on as though its virtualenv were still
    there and reports a confusing failure instead of rebuilding.
    """
    agent = agent_with_tools(["shell.exec"])
    run = submit(client, scope, agent, "build it")
    model = Recording(_tool(), _text("done"))

    await drive(engine, model, StandInSandbox(cache_state=CacheState.RESET))

    assert "sandbox_cache_reset" in await events(engine, run)
    # v2.15: told on the first call after the sandbox was taken, and only then.
    assert [request.cache_hint for request in model.requests] == [
        None,
        CacheStateHint.RESET,
    ]


async def test_a_reused_cache_says_nothing(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    agent = agent_with_tools(["shell.exec"])
    run = submit(client, scope, agent, "carry on")
    model = Recording(_tool(), _text("done"))

    await drive(engine, model, StandInSandbox(cache_state=CacheState.REUSED))

    assert "sandbox_cache_reset" not in await events(engine, run)
    assert [request.cache_hint for request in model.requests] == [None, None]


async def test_no_marker_file_is_written_into_the_workspace(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    """§11.3 forbids it, and the reason is worth keeping: a file the Agent did
    not create, in the directory it believes is its own, is the platform lying
    inside the Agent's workspace."""
    agent = agent_with_tools(["shell.exec"])
    submit(client, scope, agent, "build it")
    sandbox = StandInSandbox(cache_state=CacheState.RESET)

    await drive(engine, Recording(_tool(), _text("done")), sandbox)

    assert sandbox.calls.count("execute") == 1, "the Agent's own command, and nothing else"


# -- how a slice ends ------------------------------------------------------


async def test_a_finished_run_destroys_its_sandbox(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    """§12.3: a terminal state guarantees no live instance."""
    agent = agent_with_tools(["shell.exec"])
    submit(client, scope, agent, "answer")
    sandbox = StandInSandbox()

    await drive(engine, Recording(_tool(), _text("done")), sandbox)

    assert sandbox.calls[-1] == "destroy"
    assert "freeze" not in sandbox.calls


async def test_a_slice_boundary_freezes_and_keeps(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    """Frozen before the lease is released, per product design §16."""
    agent = agent_with_tools(["shell.exec"])
    submit(client, scope, agent, "keep going")
    sandbox = StandInSandbox()
    # A slice that expires immediately, so the first round ends it.
    await drive(engine, Recording(_tool(), _tool(), _text("done")), sandbox, seconds=0)

    assert "freeze" in sandbox.calls
    assert sandbox.calls.index("freeze") < len(sandbox.calls)
    assert "keep" in sandbox.calls
    assert "destroy" not in sandbox.calls


async def test_a_freeze_that_fails_interrupts_rather_than_requeues(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    """Product design §16: 冻结失败时 Run 进入 interrupted，不得伪装成已重新排队.

    The happy path looks identical, which is why this is easy to get wrong: a
    Run that requeues after a failed freeze leaves a container nobody owns and
    tells the console everything is fine.
    """
    agent = agent_with_tools(["shell.exec"])
    run = submit(client, scope, agent, "keep going")

    await drive(
        engine,
        Recording(_tool(), _text("done")),
        StandInSandbox(freeze_fails=True),
        seconds=0,
    )

    snapshot = client.get(f"/api/v1/runs/{run}", headers=scope).json()
    assert snapshot["status"] == "interrupted"


async def test_a_destroy_that_cannot_be_confirmed_interrupts(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    """§11.5. A Run that completed while its container may still be running is
    not a completed Run; it is one whose cleanup is unknown."""
    agent = agent_with_tools(["shell.exec"])
    run = submit(client, scope, agent, "answer")

    await drive(
        engine, Recording(_tool(), _text("done")), StandInSandbox(destroy_fails=True)
    )

    snapshot = client.get(f"/api/v1/runs/{run}", headers=scope).json()
    assert snapshot["status"] == "interrupted"


# -- shutdown --------------------------------------------------------------


class AsksUntilStopped:
    """Asks for a command every round, and asks the Worker to stop during the
    second one, the way SIGTERM lands while a round is under way."""

    def __init__(self, stop: asyncio.Event) -> None:
        self._stop = stop
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        self.calls += 1
        if self.calls == 2:
            self._stop.set()
        return _tool()


async def test_a_worker_asked_to_stop_hands_its_run_back_at_the_next_round(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    """M1 run execution design §6.5: a rolling restart returns Runs to `queued`.

    Measured 2026-10-21 before this held: `docker compose restart worker` with
    four long tasks in one process interrupted three of them, each spending
    one of its three recovery attempts, because the slice ran on to its full
    length and Compose killed the process at ten seconds.
    """
    agent = agent_with_tools(["shell.exec"])
    run = submit(client, scope, agent, "keep going")
    stop = asyncio.Event()
    model = AsksUntilStopped(stop)
    # `Any`, as in `drive`: the stand-in has only the calls these tests make.
    sandbox: Any = StandInSandbox()
    worker = WorkerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        model=model,
        notifier=NullWakeUpNotifier(),
        sandbox=sandbox,
        settings=WorkerSettings(
            worker_id="worker-stopping",
            lease_seconds=30,
            # Far longer than the test: only the stop can end this slice.
            max_slice_seconds=300,
            idle_poll_seconds=1,
        ),
    )

    await asyncio.wait_for(worker.run_forever(stop), timeout=10)

    assert model.calls == 2, "the round under way finishes; no further round starts"
    snapshot = client.get(f"/api/v1/runs/{run}", headers=scope).json()
    assert snapshot["status"] == "queued"
    history = await events(engine, run)
    assert "run_slice_ended" in history
    assert "run_interrupted" not in history
    # Handed back like any slice boundary: frozen and kept for the next Worker.
    assert "keep" in sandbox.calls
    assert "destroy" not in sandbox.calls


async def test_a_tool_call_and_its_sandbox_are_timed_by_kind(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    """Observability design §3.2: by kind, never by tool name."""
    agent = agent_with_tools(["shell.exec"])
    submit(client, scope, agent, "list")
    shell = metric("tiny_hermes_tool_call_seconds_count", kind="shell", outcome="ok")
    cold = metric("tiny_hermes_sandbox_acquire_seconds_count", outcome="cold")

    await drive(engine, Recording(_tool("ls"), _text("done")), StandInSandbox())

    assert metric("tiny_hermes_tool_call_seconds_count", kind="shell", outcome="ok") == shell + 1
    assert metric("tiny_hermes_sandbox_acquire_seconds_count", outcome="cold") == cold + 1


async def test_the_sandbox_is_asked_for_at_the_size_the_agent_declared(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    """§21.3, v2.22: the size lives on the published version, and has to
    travel from it to the acquire, or the controller sizes it by default."""
    agent = agent_with_tools(["shell.exec"], sandbox={"memory_mb": 384})
    submit(client, scope, agent, "list the files")
    sandbox = StandInSandbox()

    await drive(engine, Recording(_tool(), _text("done")), sandbox)

    assert sandbox.sizes == [384]


async def test_an_agent_that_declared_no_size_leaves_it_to_the_controller(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    agent = agent_with_tools(["shell.exec"])
    submit(client, scope, agent, "list the files")
    sandbox = StandInSandbox()

    await drive(engine, Recording(_tool(), _text("done")), sandbox)

    assert sandbox.sizes == [None]
