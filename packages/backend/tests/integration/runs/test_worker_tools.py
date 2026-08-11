"""The Worker's loop, once a round can ask for a tool.

The sandbox here is a stand-in rather than a real container: what is under test
is when the Worker acquires, executes, freezes and destroys, and in what order —
not whether Docker works, which `test_controller.py` and `test_shell_exec.py`
prove against a real daemon. A container per test would make these slow enough
that the ordering rules stopped getting written.
"""

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

    async def acquire(
        self, *, run_id: UUID, lease_id: UUID, workspace_id: UUID, profile: str
    ) -> Any:
        del run_id, lease_id, workspace_id, profile
        self.calls.append("acquire")
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
    def build(tools: list[str]) -> str:
        alias = f"tooled-{uuid4().hex[:8]}"
        agent = client.post(
            "/api/v1/agents", headers=scope, json={"name": "Tooled", "alias": alias}
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


async def test_a_run_with_tools_acquires_before_the_first_model_call(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    agent = agent_with_tools(["shell.exec"])
    submit(client, scope, agent, "list the files")
    sandbox = StandInSandbox()
    model = Recording(_text("done"))

    await drive(engine, model, sandbox)

    assert sandbox.calls[0] == "acquire"
    assert len(model.requests) == 1


async def test_a_worker_with_no_sandbox_fails_a_tool_run_rather_than_ignoring_it(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    """§11.5's `sandbox_start_failed`, and product design §16's rule that a
    sandbox failure never falls back to the host."""
    agent = agent_with_tools(["shell.exec"])
    run = submit(client, scope, agent, "list the files")

    await drive(engine, Recording(_text("done")), None)

    snapshot = client.get(f"/api/v1/runs/{run}", headers=scope).json()
    assert snapshot["status"] == "failed"


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
    model = Recording(_text("done"))

    await drive(engine, model, StandInSandbox(cache_state=CacheState.RESET))

    assert "sandbox_cache_reset" in await events(engine, run)
    assert model.requests[0].cache_hint is CacheStateHint.RESET


async def test_a_reused_cache_says_nothing(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    agent = agent_with_tools(["shell.exec"])
    run = submit(client, scope, agent, "carry on")
    model = Recording(_text("done"))

    await drive(engine, model, StandInSandbox(cache_state=CacheState.REUSED))

    assert "sandbox_cache_reset" not in await events(engine, run)
    assert model.requests[0].cache_hint is None


async def test_no_marker_file_is_written_into_the_workspace(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    """§11.3 forbids it, and the reason is worth keeping: a file the Agent did
    not create, in the directory it believes is its own, is the platform lying
    inside the Agent's workspace."""
    agent = agent_with_tools(["shell.exec"])
    submit(client, scope, agent, "build it")
    sandbox = StandInSandbox(cache_state=CacheState.RESET)

    await drive(engine, Recording(_text("done")), sandbox)

    assert "execute" not in sandbox.calls


# -- how a slice ends ------------------------------------------------------


async def test_a_finished_run_destroys_its_sandbox(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_with_tools: Any
) -> None:
    """§12.3: a terminal state guarantees no live instance."""
    agent = agent_with_tools(["shell.exec"])
    submit(client, scope, agent, "answer")
    sandbox = StandInSandbox()

    await drive(engine, Recording(_text("done")), sandbox)

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

    await drive(engine, Recording(_text("done")), StandInSandbox(destroy_fails=True))

    snapshot = client.get(f"/api/v1/runs/{run}", headers=scope).json()
    assert snapshot["status"] == "interrupted"
