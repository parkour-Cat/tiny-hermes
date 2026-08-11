"""What an Agent may run, decided twice.

Technical design §10.2. The first step decides what the model is *told about*;
the second decides what actually runs, against the real arguments. They are not
the same check and the second is the one that matters: a model can ask for
anything regardless of what it was told, so a platform relying on the schema
list to keep a tool unreachable would be relying on the model's good manners.

Pure. Nothing here opens a socket or reads a database — it turns a call the
model made into either a command the Controller will accept or a named refusal.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.sandbox.domain.command import (
    ALLOWED_WORKING_DIRECTORIES,
    SandboxCommand,
)

#: Every tool this platform implements. An AgentVersion may bind a subset;
#: publishing refuses a name outside it, so a Run cannot fail on its first call
#: for a reason its author could have been told about.
IMPLEMENTED_TOOLS = ("shell.exec",)

DEFAULT_WORKING_DIRECTORY = "/workspace/data"
DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 900
DEFAULT_OUTPUT_BYTES = 1_048_576

SHELL_EXEC_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "shell.exec",
        "description": (
            "Run one shell command inside this Run's sandbox. The sandbox has "
            "no network at all, so anything that fetches from the internet will "
            "fail. The filesystem is read-only except /workspace/data and "
            "/workspace/cache. The command runs as a non-root user."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command line, interpreted by bash.",
                },
                "cwd": {
                    "type": "string",
                    "description": (
                        "Working directory. Must be inside /workspace/data or "
                        "/workspace/cache. Defaults to /workspace/data."
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": f"Up to {MAX_TIMEOUT_SECONDS}. Defaults to "
                    f"{DEFAULT_TIMEOUT_SECONDS}.",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}

SHELL_EXEC_ARGUMENTS = frozenset({"command", "cwd", "timeout_seconds"})


class RefusalReason(StrEnum):
    NOT_AUTHORIZED = "tool_not_authorized"
    UNKNOWN_TOOL = "unknown_tool"
    INVALID_ARGUMENTS = "invalid_arguments"
    WORKING_DIRECTORY_NOT_ALLOWED = "working_directory_not_allowed"
    TIMEOUT_TOO_LONG = "timeout_too_long"


class ToolRefused(Exception):
    """A call the platform will not run, and the call it answers.

    The `call_id` travels with the refusal so the loop can send a result back.
    A model left waiting on a call that never gets an answer will either retry
    it or invent what it returned.
    """

    def __init__(self, reason: RefusalReason, call_id: str, detail: str = "") -> None:
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)
        self.reason = reason
        self.call_id = call_id
        self.detail = detail


@dataclass(frozen=True)
class AuthorizedCall:
    call_id: str
    name: str
    command: SandboxCommand


def schemas_for(bound: tuple[str, ...]) -> list[dict[str, Any]]:
    """Step one: what the model is told exists.

    An Agent that binds nothing advertises nothing, so a model that never heard
    of `shell.exec` cannot correctly ask for it — which is worth having even
    though step two is what enforces it.
    """
    return [SHELL_EXEC_SCHEMA for name in bound if name == "shell.exec"]


def authorize(*, bound: tuple[str, ...], call: ToolCallBlock) -> AuthorizedCall:
    """Step two: what actually runs, against the arguments that arrived."""
    if call.name not in IMPLEMENTED_TOOLS:
        raise ToolRefused(RefusalReason.UNKNOWN_TOOL, call.call_id, call.name)
    if call.name not in bound:
        # Nothing about the call may be wrong except this. The schema list the
        # model was handed is not a control; this is.
        raise ToolRefused(RefusalReason.NOT_AUTHORIZED, call.call_id, call.name)
    return AuthorizedCall(
        call_id=call.call_id, name=call.name, command=_shell_command(call)
    )


def _shell_command(call: ToolCallBlock) -> SandboxCommand:
    arguments = call.arguments
    unexpected = set(arguments) - SHELL_EXEC_ARGUMENTS
    if unexpected:
        # Dropping it silently would leave a model believing it got what it
        # asked for, which is worse than being told no.
        raise ToolRefused(
            RefusalReason.INVALID_ARGUMENTS, call.call_id, f"unexpected: {sorted(unexpected)}"
        )

    command: Any = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ToolRefused(RefusalReason.INVALID_ARGUMENTS, call.call_id, "command")

    cwd: Any = arguments.get("cwd", DEFAULT_WORKING_DIRECTORY)
    if not isinstance(cwd, str) or not _inside_workspace(cwd):
        raise ToolRefused(RefusalReason.WORKING_DIRECTORY_NOT_ALLOWED, call.call_id, str(cwd))

    timeout: Any = arguments.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise ToolRefused(RefusalReason.INVALID_ARGUMENTS, call.call_id, "timeout_seconds")
    if timeout > MAX_TIMEOUT_SECONDS:
        # Refused rather than clamped: a clamp runs a different command than
        # the model asked for and says nothing about it.
        raise ToolRefused(RefusalReason.TIMEOUT_TOO_LONG, call.call_id, str(timeout))

    return SandboxCommand(
        # The whole line goes to a shell that owns the quoting. A platform that
        # split it would own every quoting bug of every command any Agent ever
        # writes, and would get them subtly wrong.
        argv=["/bin/bash", "-lc", command],
        cwd=cwd,
        timeout_seconds=timeout,
        output_limit=DEFAULT_OUTPUT_BYTES,
    )


def _inside_workspace(cwd: str) -> bool:
    """Normalized before comparing, so `..` cannot walk out of an allowed root.

    A prefix test alone would also accept `/workspace/datax`, which is not the
    data mount and is a directory an Agent could create.
    """
    if not cwd.startswith("/"):
        return False
    parts: list[str] = []
    for piece in cwd.split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            if not parts:
                return False
            parts.pop()
            continue
        parts.append(piece)
    resolved = "/" + "/".join(parts)
    return any(
        resolved == allowed or resolved.startswith(f"{allowed}/")
        for allowed in ALLOWED_WORKING_DIRECTORIES
    )
