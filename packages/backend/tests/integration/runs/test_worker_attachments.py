"""A file somebody sent reaches the sandbox's inbox before the Agent looks
(§1.27, v2.27): fetched by the Worker, written with the same helper
`file.write` uses, once per Session."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings, WorkspaceRuntime
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.sandbox.domain.command import CommandResult, SandboxCommand
from tiny_hermes.session_workspace.domain.models import WorkspaceQuota
from tiny_hermes.session_workspace.infrastructure.minio_store import MinioObjectStore
from tiny_hermes.tools.domain.files import FILE_HELPER

from . import test_worker_workspace as _workspace
from .test_worker_workspace import (  # pyright: ignore[reportPrivateUsage]
    GatewaySandbox,
    Recording,
    _events_of,  # pyright: ignore[reportPrivateUsage]
    _run_row,  # pyright: ignore[reportPrivateUsage]
    _submit,  # pyright: ignore[reportPrivateUsage]
    _tool,  # pyright: ignore[reportPrivateUsage]
)

# pytest finds a fixture by the module attribute it is bound to.
objects = _workspace.objects
tooled_agent = _workspace.tooled_agent

BODY = b"PK\x03\x04 the bytes of a spreadsheet"


@dataclass
class CommandLog(GatewaySandbox):
    """The gateway sandbox, keeping every command and its stdin in order."""

    commands: list[tuple[list[str], bytes | None]] = field(default_factory=list[Any])

    async def execute(self, *, command: SandboxCommand, **kwargs: Any) -> CommandResult:
        self.commands.append((command.argv, command.stdin))
        return await super().execute(command=command, **kwargs)


@dataclass
class Attachments:
    """Feishu, as far as the Worker can tell: bytes for a reference, or no."""

    body: bytes | None = BODY
    asked: list[tuple[str, UUID]] = field(default_factory=list[Any])

    async def file_bytes_for(self, reference: str, session_id: UUID) -> bytes:
        self.asked.append((reference, session_id))
        if self.body is None:
            raise LookupError("this session has no binding that can fetch files")
        return self.body


async def _attach(engine: AsyncEngine, run_id: str, name: str = "a.xlsx") -> None:
    """The file part ingestion writes beside the person's words."""
    async with engine.begin() as db:
        row = (
            await db.execute(
                text(
                    "SELECT id, content FROM session_messages "
                    "WHERE source_run_id = :run AND role = 'user'"
                ),
                {"run": run_id},
            )
        ).one()
        content = dict(row.content)
        content["parts"] = [
            *content["parts"],
            {"type": "file", "reference": "feishu:om_1:file_k", "name": name},
        ]
        await db.execute(
            text("UPDATE session_messages SET content = CAST(:c AS json) WHERE id = :id"),
            {"c": json.dumps(content), "id": row.id},
        )


async def _slice(
    engine: AsyncEngine, model: Any, sandbox: Any, objects: MinioObjectStore, files: Any
) -> None:
    await WorkerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        model=model,
        notifier=NullWakeUpNotifier(),
        sandbox=sandbox,
        workspace=WorkspaceRuntime(
            objects=objects,
            quota=WorkspaceQuota(max_bytes=10_000_000, max_objects=10_000),
            staging_ttl_seconds=3_600,
            export_limit=100 * 1024 * 1024,
        ),
        files=files,
        settings=WorkerSettings(
            worker_id="worker-files", lease_seconds=30, max_slice_seconds=30, idle_poll_seconds=1
        ),
    ).run_once()


def _is_write(argv: list[str]) -> bool:
    return argv[0] == FILE_HELPER and "write" in argv


def _writes(sandbox: CommandLog) -> list[tuple[str, bytes | None]]:
    """(path, body) for each helper write, in order."""
    return [
        (argv[argv.index("write") + 1], stdin)
        for argv, stdin in sandbox.commands
        if _is_write(argv)
    ]


async def test_a_file_the_run_received_is_in_the_inbox_before_the_agent_s_first_command(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    objects: MinioObjectStore,
    tooled_agent: Callable[[list[str]], str],
) -> None:
    run, _ = _submit(client, scope, tooled_agent(["shell.exec"]))
    await _attach(engine, run)
    sandbox = CommandLog()
    attachments = Attachments()

    await _slice(engine, Recording(sandbox, _tool("ls inbox")), sandbox, objects, attachments)

    assert _writes(sandbox) == [("inbox/a.xlsx", BODY)]
    placed = next(i for i, (argv, _) in enumerate(sandbox.commands) if _is_write(argv))
    agent_s = next(i for i, (argv, _) in enumerate(sandbox.commands) if "ls inbox" in argv)
    assert placed < agent_s
    assert [reference for reference, _ in attachments.asked] == ["feishu:om_1:file_k"]
    assert (await _run_row(engine, run)).status == "completed"


async def test_a_file_that_cannot_be_fetched_is_said_to_the_model_not_skipped(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    objects: MinioObjectStore,
    tooled_agent: Callable[[list[str]], str],
) -> None:
    """A missing inbox file the Agent was told is there would be searched
    for, guessed at, and then described as if it had been read."""
    run, _ = _submit(client, scope, tooled_agent(["shell.exec"]))
    await _attach(engine, run)
    sandbox = CommandLog()
    model = Recording(sandbox, _tool("ls inbox"))

    await _slice(engine, model, sandbox, objects, Attachments(body=None))

    assert _writes(sandbox) == []
    # In the words the model reads: a note, or a tool result's output.
    told = " ".join(
        str(getattr(block, "text", "")) + str(getattr(block, "output", ""))
        for request in model.requests
        for message in request.messages
        for block in message.blocks
    )
    assert "a.xlsx" in told and "could not be fetched" in told
    assert "run_failed" not in await _events_of(engine, run)


async def test_a_file_placed_once_is_not_fetched_again(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    objects: MinioObjectStore,
    tooled_agent: Callable[[list[str]], str],
) -> None:
    """It is in the saved workspace from then on; fetching it again would
    overwrite whatever the Agent made of it since."""
    agent = tooled_agent(["shell.exec"])
    first, session = _submit(client, scope, agent)
    await _attach(engine, first, "old.xlsx")
    earlier = GatewaySandbox()
    await _slice(engine, Recording(earlier, _tool()), earlier, objects, Attachments())
    second, _ = _submit(client, scope, agent, session_id=session)
    sandbox = CommandLog()
    attachments = Attachments()

    await _slice(engine, Recording(sandbox, _tool("ls")), sandbox, objects, attachments)

    assert _writes(sandbox) == []
    assert attachments.asked == []
    assert (await _run_row(engine, second)).status == "completed"


async def test_a_file_sent_on_its_own_is_placed_by_the_run_that_first_needs_a_sandbox(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    objects: MinioObjectStore,
    tooled_agent: Callable[[list[str]], str],
) -> None:
    """A Feishu file arrives as a message of its own, and its Run usually
    just answers 「收到」 — no command, so no sandbox. The work is asked for
    in the next message, and that Run must find the file where the note said."""
    agent = tooled_agent(["shell.exec"])
    first, session = _submit(client, scope, agent)
    await _attach(engine, first, "sent-alone.xlsx")
    words_only = Attachments()
    await _slice(engine, Recording(GatewaySandbox()), GatewaySandbox(), objects, words_only)
    assert words_only.asked == []
    second, _ = _submit(client, scope, agent, session_id=session)
    sandbox = CommandLog()

    await _slice(engine, Recording(sandbox, _tool("ls inbox")), sandbox, objects, Attachments())

    assert _writes(sandbox) == [("inbox/sent-alone.xlsx", BODY)]
    assert (await _run_row(engine, second)).status == "completed"
