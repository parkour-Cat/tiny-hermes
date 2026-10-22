"""A tool that may have reached the outside makes its round unsafe to replay.

Crash recovery re-runs an interrupted Run from its last checkpoint when that
checkpoint says replay is safe. Only the model call used to be able to say
otherwise. A round whose `http.*` POST reached the far end, followed by a
Worker killed before the round was recorded, was replayed: the model asked
again from the same transcript and the write went out again, up to
`max_recovery_attempts` more times.

So a call that may have an effect outside the sandbox marks the Run's
checkpoint effect unknown before it is sent, in the transaction that charges
it; recording the round writes the checkpoint again. These tests read the
column at the moment the call happens, and after the round.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.runs.ports.model import ModelResponse, StopReason
from tiny_hermes.sandbox.domain.command import CommandResult, SandboxCommand

from ..conftest import VALID_SPEC
from ..egress_support import ProxyHandle
from .http_tool_support import StandIn, approve_host, ask, register_tool, worker
from .test_http_tool_calls import _agent as http_agent  # pyright: ignore[reportPrivateUsage]
from .test_worker_tools import (
    Recording,
    StandInSandbox,
    drive,
    submit,
)
from .test_worker_tools import _tool as shell_call  # pyright: ignore[reportPrivateUsage]


async def _effect(engine: AsyncEngine, run_id: str) -> str:
    async with engine.connect() as connection:
        return str(
            await connection.scalar(
                text("SELECT checkpoint_effect_status FROM runs WHERE id = :id"),
                {"id": UUID(run_id)},
            )
        )


@dataclass
class WatchingSandbox(StandInSandbox):
    """Reads the Run's checkpoint effect while the command is running."""

    engine: Any = None
    run_id: str = ""
    seen: list[str] = field(default_factory=list[str])

    async def execute(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID, command: SandboxCommand
    ) -> CommandResult:
        self.seen.append(await _effect(self.engine, self.run_id))
        return await super().execute(
            run_id=run_id, lease_id=lease_id, sandbox_id=sandbox_id, command=command
        )


def _shell_agent(client: TestClient, scope: dict[str, str], network: list[str]) -> str:
    alias = f"effects-{uuid4().hex[:8]}"
    agent = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Effects", "alias": alias}
    ).json()
    spec: dict[str, Any] = {**VALID_SPEC, "tools": ["shell.exec"]}
    if network:
        spec["network"] = {"allow": network}
    draft = client.put(
        f"/api/v1/agents/{agent['id']}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": spec},
    )
    assert draft.status_code == 200, draft.text
    published = client.post(
        f"/api/v1/agents/{agent['id']}/publish",
        headers=scope,
        json={"expected_revision": draft.json()["revision"]},
    )
    assert published.status_code == 201, published.text
    return str(agent["id"])


async def test_a_command_with_the_network_marks_the_round_unsafe_while_it_runs(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine
) -> None:
    approve_host(client, scope, "api.example.com")
    run = submit(client, scope, _shell_agent(client, scope, ["api.example.com"]), "go")
    sandbox = WatchingSandbox(engine=engine, run_id=run)

    post = shell_call("curl -X POST https://api.example.com")
    await drive(engine, Recording(post, _done()), sandbox)

    assert sandbox.seen == ["unknown"]
    # The round was recorded with its result: replaying from there no longer
    # repeats the command.
    assert await _effect(engine, run) == "none"


async def test_a_command_with_no_network_leaves_the_round_safe(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine
) -> None:
    """Everything it can change is in the workspace, which recovery restores
    from the last committed revision."""
    run = submit(client, scope, _shell_agent(client, scope, []), "go")
    sandbox = WatchingSandbox(engine=engine, run_id=run)

    await drive(engine, Recording(shell_call("ls"), _done()), sandbox)

    assert sandbox.seen == ["none"]


@pytest.mark.parametrize("operation", ["listOrders", "createOrder"])
async def test_an_http_call_marks_the_round_unsafe_before_it_is_sent(
    operation: str,
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """A GET too: whether a read changes nothing is the far end's promise,
    and the platform cannot check it."""
    stand_in, url = api
    approve_host(client, scope, "127.0.0.1")
    version_id = register_tool(client, scope, url)
    policy = "preauthorized" if operation == "createOrder" else None
    agent_id = http_agent(client, scope, version_id, [operation], ["127.0.0.1"], policy)
    run = ask(client, scope, session_for(agent_id), f"http.orders.{operation}")
    seen: list[str] = []

    async def look() -> None:
        seen.append(await _effect(engine, str(run["id"])))

    stand_in.on_request = look

    await worker(engine, scope["X-Workspace-Id"], proxy).run_once()

    assert seen == ["unknown"]


def _done() -> ModelResponse:
    return ModelResponse(stop_reason=StopReason.COMPLETED, text="done")
