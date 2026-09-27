"""Consecutive sandbox reads in one reply run at once.

A model that wants three files asks for three `file.read` calls in one reply;
they used to run one after another. Reads change nothing, and the Controller
serves two executes on one sandbox at the same time
(`sandbox/test_controller.py::test_two_commands_on_one_sandbox_run_at_once…`),
so a run of consecutive reads is sent together. Only consecutive: a read
after a `shell.exec` in the same reply must see what the command wrote.
"""

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.runs.ports.model import ModelResponse, StopReason
from tiny_hermes.sandbox.domain.command import CommandResult, SandboxCommand

from ..conftest import VALID_SPEC
from .test_worker_tools import StandInSandbox, drive, submit


@dataclass
class SlowSandbox(StandInSandbox):
    """Each execute takes a moment, and the most that ever overlapped is kept."""

    running: int = 0
    most_at_once: int = 0
    order: list[str] | None = None

    async def execute(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID, command: SandboxCommand
    ) -> CommandResult:
        del run_id, lease_id, sandbox_id
        self.running += 1
        self.most_at_once = max(self.most_at_once, self.running)
        try:
            await asyncio.sleep(0.2)
            if self.order is not None:
                self.order.append(" ".join(command.argv))
            return CommandResult(exit_code=0, output="{}", truncated=False, timed_out=False)
        finally:
            self.running -= 1


@pytest.fixture
def agent(client: TestClient, scope: dict[str, str]) -> str:
    created = client.post(
        "/api/v1/agents",
        headers=scope,
        json={"name": "Reader", "alias": f"reader-{uuid4().hex[:8]}"},
    ).json()
    spec = {**VALID_SPEC, "tools": ["file.read", "file.list", "shell.exec"]}
    draft = client.put(
        f"/api/v1/agents/{created['id']}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": spec},
    ).json()
    published = client.post(
        f"/api/v1/agents/{created['id']}/publish",
        headers=scope,
        json={"expected_revision": draft["revision"]},
    )
    assert published.status_code == 201, published.text
    return str(created["id"])


class Once:
    def __init__(self, *calls: ToolCallBlock) -> None:
        self._calls = calls
        self.requests: list[Any] = []

    async def complete(self, request: Any) -> ModelResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            return ModelResponse(
                stop_reason=StopReason.TOOL_CALL,
                text="",
                tool_calls=self._calls,
                input_tokens=10,
                output_tokens=5,
            )
        return ModelResponse(
            stop_reason=StopReason.COMPLETED, text="done", input_tokens=10, output_tokens=5
        )


def _read(call_id: str, path: str) -> ToolCallBlock:
    return ToolCallBlock(call_id=call_id, name="file.read", arguments={"path": path})


async def test_consecutive_reads_run_at_once_and_answer_in_order(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent: str
) -> None:
    run = submit(client, scope, agent, "read three files")
    sandbox = SlowSandbox()
    model = Once(_read("r1", "a.md"), _read("r2", "b.md"), _read("r3", "c.md"))

    await drive(engine, model, sandbox)

    assert sandbox.most_at_once == 3
    answered = [
        block.call_id
        for message in model.requests[1].messages
        for block in message.blocks
        if getattr(block, "call_id", None) and message.role == "tool"
    ]
    assert answered == ["r1", "r2", "r3"]
    body = client.get(f"/api/v1/runs/{run}", headers=scope).json()
    assert body["budget"]["consumed_tool_calls"] == 3


async def test_a_read_after_a_command_waits_for_it(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent: str
) -> None:
    submit(client, scope, agent, "write then read")
    sandbox = SlowSandbox(order=[])
    model = Once(
        ToolCallBlock(call_id="w", name="shell.exec", arguments={"command": "make report"}),
        _read("r1", "report.md"),
        _read("r2", "notes.md"),
    )

    await drive(engine, model, sandbox)

    assert sandbox.order is not None
    assert "make report" in sandbox.order[0]
    # The two reads after it may overlap each other, not the command.
    assert sandbox.most_at_once == 2
