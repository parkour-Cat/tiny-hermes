"""Long shell.exec output becomes an Artifact identifier in the tool result.

The recorder already exists; this is the seam that feeds it. Bytes past the
inline preview are stored, and the model is told `artifact_id` — never a
bucket key — so a later download can be authorized without teaching the Agent
where objects live.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from tiny_hermes.artifacts.application.service import ArtifactLimitExceeded
from tiny_hermes.artifacts.domain.models import Artifact
from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.sandbox.domain.command import (
    CommandResult,
    SandboxCommand,
    StreamedResult,
)
from tiny_hermes.tools.application.execute import run_tool_call

RUN = uuid4()
LEASE = uuid4()
SANDBOX = uuid4()
PREVIEW = 32


def _call(name: str = "shell.exec", **arguments: Any) -> ToolCallBlock:
    if name == "shell.exec" and "command" not in arguments:
        arguments = {"command": "yes | head -c 100", **arguments}
    return ToolCallBlock(call_id="c1", name=name, arguments=arguments)


@dataclass
class FakeRecorder:
    """An ArtifactRecorder stand-in: bytes in, a row-shaped Artifact out."""

    fail: bool = False
    chunks: list[bytes] = field(default_factory=list[bytes])
    artifact_id: UUID = field(default_factory=uuid4)
    finished: bool = False

    async def deliver(self, chunk: bytes) -> None:
        if self.fail:
            raise ArtifactLimitExceeded("the run's artifact budget is spent")
        self.chunks.append(chunk)

    async def finish(self, *, truncated: bool) -> Artifact | None:
        self.finished = True
        if self.fail:
            raise ArtifactLimitExceeded("the run's artifact budget is spent")
        body = b"".join(self.chunks)
        return Artifact(
            id=self.artifact_id,
            workspace_id=uuid4(),
            session_id=uuid4(),
            run_id=RUN,
            object_key=f"workspaces/x/runs/{RUN}/artifacts/{self.artifact_id}",
            filename="command-output.log",
            media_type="text/plain",
            size_bytes=len(body),
            sha256=sha256(body).hexdigest(),
            truncated=truncated,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )


@dataclass
class FakeSandbox:
    """Controller + streamer, so the test can see which path ran."""

    output: bytes = b""
    exit_code: int = 0
    timed_out: bool = False
    calls: list[str] = field(default_factory=list[str])

    async def execute(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID, command: SandboxCommand
    ) -> CommandResult:
        del run_id, lease_id, sandbox_id, command
        self.calls.append("execute")
        return CommandResult(
            exit_code=self.exit_code,
            output=self.output.decode("utf-8", errors="replace"),
            truncated=False,
            timed_out=self.timed_out,
        )

    async def execute_stream(
        self,
        *,
        run_id: UUID,
        lease_id: UUID,
        sandbox_id: UUID,
        command: SandboxCommand,
        artifact_limit: int,
        sink: Any,
    ) -> StreamedResult:
        del run_id, lease_id, sandbox_id, command
        self.calls.append("execute_stream")
        observed = 0
        delivered = 0
        for start in range(0, len(self.output), 13):
            chunk = self.output[start : start + 13]
            observed += len(chunk)
            room = artifact_limit - delivered
            if room > 0:
                portion = chunk[:room]
                await sink(portion)
                delivered += len(portion)
        return StreamedResult(
            exit_code=124 if self.timed_out else self.exit_code,
            timed_out=self.timed_out,
            observed_bytes=observed,
            delivered_bytes=delivered,
            truncated=observed > delivered,
        )


async def _run(
    sandbox: FakeSandbox,
    call: ToolCallBlock,
    *,
    recorder: FakeRecorder | None = None,
    preview_limit: int = PREVIEW,
    bound: tuple[str, ...] = ("shell.exec", "file.write"),
) -> Any:
    return await run_tool_call(
        controller=sandbox,
        run_id=RUN,
        lease_id=LEASE,
        sandbox_id=SANDBOX,
        bound=bound,
        call=call,
        streamer=sandbox,
        open_artifact=None if recorder is None else (lambda: recorder),
        preview_limit=preview_limit,
    )


async def test_output_inside_the_preview_stays_inline_and_opens_no_artifact() -> None:
    sandbox = FakeSandbox(output=b"hello\n")
    recorder = FakeRecorder()

    result = await _run(sandbox, _call(), recorder=recorder)

    assert sandbox.calls == ["execute_stream"]
    assert result.output.strip() == "hello"
    assert "artifact_id=" not in result.output
    assert recorder.chunks == []
    assert recorder.finished is False


async def test_output_past_the_preview_names_an_artifact_not_a_store_key() -> None:
    body = b"a" * 80
    sandbox = FakeSandbox(output=body)
    recorder = FakeRecorder()

    result = await _run(sandbox, _call(), recorder=recorder)

    assert result.output.startswith("a" * PREVIEW)
    assert f"artifact_id={recorder.artifact_id}" in result.output
    assert "artifact_truncated=false" in result.output
    assert "workspaces/" not in result.output
    assert "artifacts/" not in result.output
    assert b"".join(recorder.chunks) == body
    assert recorder.finished is True


async def test_a_failed_artifact_upload_keeps_the_preview_and_names_the_failure() -> None:
    sandbox = FakeSandbox(output=b"b" * 80)
    recorder = FakeRecorder(fail=True)

    result = await _run(sandbox, _call(), recorder=recorder)

    assert result.output.startswith("b" * PREVIEW)
    assert "artifact_store_failed" in result.output
    assert "artifact_id=" not in result.output
    assert "workspaces/" not in result.output
    assert result.failed is False


async def test_an_artifact_past_the_ceiling_is_marked_truncated() -> None:
    sandbox = FakeSandbox(output=b"c" * 80)
    recorder = FakeRecorder()

    result = await run_tool_call(
        controller=sandbox,
        run_id=RUN,
        lease_id=LEASE,
        sandbox_id=SANDBOX,
        bound=("shell.exec",),
        call=_call(),
        streamer=sandbox,
        open_artifact=lambda: recorder,
        preview_limit=PREVIEW,
        artifact_limit=50,
    )

    assert "artifact_id=" in result.output
    assert "artifact_truncated=true" in result.output
    assert b"".join(recorder.chunks) == b"c" * 50


async def test_a_timeout_on_the_stream_is_still_a_result() -> None:
    sandbox = FakeSandbox(output=b"partial\n", timed_out=True)

    result = await _run(sandbox, _call(timeout_seconds=1), recorder=None)

    assert "timed out" in result.output.lower()
    assert result.failed is False
    assert result.exit_code != 0


async def test_file_write_still_uses_the_buffered_execute_path() -> None:
    sandbox = FakeSandbox(output=b"wrote")

    result = await _run(
        sandbox,
        _call("file.write", path="note.txt", content="hi"),
        recorder=FakeRecorder(),
    )

    assert sandbox.calls == ["execute"]
    assert result.output == "wrote"
    assert "artifact_id=" not in result.output


async def test_without_a_streamer_shell_exec_keeps_the_buffered_path() -> None:
    sandbox = FakeSandbox(output=b"short\n")

    result = await run_tool_call(
        controller=sandbox,
        run_id=RUN,
        lease_id=LEASE,
        sandbox_id=SANDBOX,
        bound=("shell.exec",),
        call=_call(),
    )

    assert sandbox.calls == ["execute"]
    assert result.output.strip() == "short"


@pytest.mark.parametrize("marker", ["s3://", "minio", "object_key"])
async def test_the_tool_result_never_echoes_storage_vocabulary(marker: str) -> None:
    recorder = FakeRecorder()
    result = await _run(FakeSandbox(output=b"d" * 80), _call(), recorder=recorder)
    assert marker not in result.output.lower()
