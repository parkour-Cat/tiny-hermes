"""A long shell.exec, through the Worker, is a tenant-scoped Artifact.

3C proved the recorder and the routes in isolation. This is the missing wire:
the tool result names `artifact_id`, never a store key, and the same caller
who submitted the Run can download the bytes.
"""

import json
import os
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.worker import (
    WorkerRuntime,
    WorkerSettings,
    WorkspaceRuntime,
)
from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.ports.model import ModelResponse, StopReason
from tiny_hermes.sandbox.domain.command import StreamedResult
from tiny_hermes.session_workspace.domain.models import WorkspaceQuota
from tiny_hermes.session_workspace.infrastructure.minio_store import MinioObjectStore

from ..conftest import VALID_SPEC
from ..runs.test_worker_tools import Recording
from ..runs.test_worker_workspace import GatewaySandbox

PREVIEW = 64
BODY = b"artifact-body-" + b"x" * 200
ARTIFACT_ID = re.compile(r"artifact_id=([0-9a-f-]{36})")


def _text(said: str) -> ModelResponse:
    return ModelResponse(stop_reason=StopReason.COMPLETED, text=said)


def _tool(command: str) -> ModelResponse:
    return ModelResponse(
        stop_reason=StopReason.TOOL_CALL,
        text="",
        tool_calls=(
            ToolCallBlock(call_id="c1", name="shell.exec", arguments={"command": command}),
        ),
    )


def _submit(
    client: TestClient, scope: dict[str, str], agent_id: str
) -> tuple[str, str]:
    session_id = str(
        client.post(
            "/api/v1/sessions", headers=scope, json={"agent_id": agent_id}
        ).json()["id"]
    )
    run_id = str(
        client.post(
            "/api/v1/runs",
            headers={**scope, "Idempotency-Key": uuid4().hex},
            json={"session_id": session_id, "input": "work with files"},
        ).json()["id"]
    )
    return run_id, session_id


@pytest.fixture(scope="module")
def objects() -> MinioObjectStore:
    return MinioObjectStore(
        endpoint=os.environ.get("S3_ENDPOINT", "http://localhost:9000"),
        access_key=os.environ.get("S3_ACCESS_KEY", "tiny-hermes-local"),
        secret_key=os.environ.get("S3_SECRET_KEY", "tiny-hermes-local-password"),
        bucket=os.environ.get("S3_BUCKET", "tiny-hermes-test"),
    )


@pytest.fixture(autouse=True)
async def reachable(objects: MinioObjectStore) -> None:
    parsed = urlparse(os.environ.get("S3_ENDPOINT", "http://localhost:9000"))
    try:
        socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or 9000), timeout=1
        ).close()
    except OSError as unreachable:  # pragma: no cover - environment
        pytest.skip(f"no reachable MinIO: {unreachable}")
    await objects.ensure_bucket()


@pytest.fixture
def tooled_agent(client: TestClient, scope: dict[str, str]) -> Callable[[list[str]], str]:
    def build(tools: list[str]) -> str:
        alias = f"art-{uuid4().hex[:8]}"
        agent = client.post(
            "/api/v1/agents", headers=scope, json={"name": "Artifacts", "alias": alias}
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


@dataclass
class StreamingSandbox(GatewaySandbox):
    """The workspace fake, plus the sink-based stream `SandboxClient` exposes."""

    output: bytes = b""

    async def execute_stream(
        self,
        *,
        artifact_limit: int,
        sink: Callable[[bytes], Any],
        **_: Any,
    ) -> StreamedResult:
        self.calls.append("execute_stream")
        observed = len(self.output)
        delivered = min(observed, artifact_limit)
        await sink(self.output[:delivered])
        return StreamedResult(
            exit_code=0,
            timed_out=False,
            observed_bytes=observed,
            delivered_bytes=delivered,
            truncated=observed > delivered,
        )


async def _drive(
    engine: AsyncEngine,
    model: Recording,
    sandbox: StreamingSandbox,
    objects: MinioObjectStore,
    *,
    preview_bytes: int = PREVIEW,
    artifact_max_bytes: int = 1024,
) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    await WorkerRuntime(
        session_factory=sessions,
        model=model,
        notifier=NullWakeUpNotifier(),
        sandbox=sandbox,
        workspace=WorkspaceRuntime(
            objects=objects,
            quota=WorkspaceQuota(max_bytes=10_000_000, max_objects=10_000),
            staging_ttl_seconds=3_600,
            export_limit=100 * 1024 * 1024,
            artifact_max_bytes=artifact_max_bytes,
            run_artifact_max_bytes=2048,
            preview_bytes=preview_bytes,
        ),
        settings=WorkerSettings(
            worker_id="worker-artifacts",
            lease_seconds=30,
            max_slice_seconds=30,
            idle_poll_seconds=1,
        ),
    ).run_once()


async def _tool_output(engine: AsyncEngine, run_id: str) -> str:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT m.content FROM session_messages m "
                "JOIN runs r ON r.session_id = m.session_id "
                "WHERE r.id = :run AND m.role = 'tool' ORDER BY m.sequence"
            ),
            {"run": run_id},
        )
        found = rows.first()
    assert found is not None
    raw: Any = found.content
    document = cast(dict[str, Any], raw if isinstance(raw, dict) else json.loads(str(raw)))
    parts: list[dict[str, Any]] = list(document["parts"])
    output = next(part["output"] for part in parts if part["type"] == "tool_result")
    return str(output)


async def test_long_shell_output_downloads_byte_for_byte_from_the_artifact_route(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    objects: MinioObjectStore,
    tooled_agent: Callable[[list[str]], str],
) -> None:
    agent = tooled_agent(["shell.exec"])
    run_id, _session = _submit(client, scope, agent)
    sandbox = StreamingSandbox(output=BODY)
    model = Recording(_tool("printf long"), _text("stored"))

    await _drive(engine, model, sandbox, objects)

    output = await _tool_output(engine, run_id)
    matched = ARTIFACT_ID.search(output)
    assert matched is not None, output
    artifact_id = UUID(matched.group(1))
    assert output.startswith(BODY[:PREVIEW].decode())
    assert "artifact_truncated=false" in output
    assert "workspaces/" not in output
    assert sandbox.calls.count("execute_stream") == 1
    assert "execute" not in sandbox.calls

    downloaded = client.get(f"/api/v1/artifacts/{artifact_id}/content", headers=scope)
    assert downloaded.status_code == 200
    assert downloaded.content == BODY

    shown = client.get(f"/api/v1/artifacts/{artifact_id}", headers=scope)
    assert shown.status_code == 200
    assert shown.json()["size_bytes"] == len(BODY)
    assert shown.json()["truncated"] is False
    assert "object_key" not in shown.json()

    assert client.get(f"/api/v1/runs/{run_id}", headers=scope).json()["status"] == "completed"


async def test_short_shell_output_does_not_open_an_artifact(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    objects: MinioObjectStore,
    tooled_agent: Callable[[list[str]], str],
) -> None:
    agent = tooled_agent(["shell.exec"])
    run_id, _session = _submit(client, scope, agent)
    sandbox = StreamingSandbox(output=b"ok\n")
    model = Recording(_tool("echo ok"), _text("done"))

    await _drive(engine, model, sandbox, objects)

    output = await _tool_output(engine, run_id)
    assert "ok" in output
    assert "artifact_id=" not in output
    async with engine.connect() as connection:
        count = await connection.scalar(text("SELECT count(*) FROM artifacts"))
    assert int(count or 0) == 0
