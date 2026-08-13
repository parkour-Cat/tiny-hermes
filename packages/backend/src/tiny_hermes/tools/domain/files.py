"""The three file verbs, translated into helper invocations. Pure.

Design §5.2 and §10: `file.*` never trusts a checked path string — the path is
normalized here with the same one rule the checkpoint uses, and then the
openat2 helper inside the image resolves it beneath the data root in the
kernel. No user string is interpolated into anything a shell reads; the argv
is the helper's, and the write body travels on stdin.
"""

from typing import Any

from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.sandbox.domain.command import SandboxCommand
from tiny_hermes.session_workspace.domain.manifest import (
    InvalidWorkspacePath,
    normalize_workspace_path,
)

FILE_HELPER = "/usr/local/bin/tiny-hermes-file-helper"
DATA_ROOT = "/workspace/data"

#: Design §15's per-call defaults. Deliberately module constants equal to the
#: settings defaults: the registry is pure, and an operator override arrives
#: with the wiring slice that can thread it.
READ_LIMIT_BYTES = 1_048_576
WRITE_LIMIT_BYTES = 16_777_216
LIST_LIMIT_ENTRIES = 1_000

#: The helper's exit code for "the file kept going past your limit".
TRUNCATED_EXIT = 3

FILE_TOOL_TIMEOUT_SECONDS = 30

FILE_ARGUMENTS = {
    "file.read": frozenset({"path"}),
    "file.write": frozenset({"path", "content"}),
    "file.list": frozenset({"path", "offset", "limit"}),
}


class FilePathRefused(Exception):
    """A path the data root will never serve, refused before any syscall."""


class FileArgumentsInvalid(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def file_tool_command(call: ToolCallBlock) -> SandboxCommand:
    """The helper invocation for one authorized file call.

    Raises ``FilePathRefused`` for any path that could name something outside
    the data root, and ``FileArgumentsInvalid`` for everything else wrong —
    the registry maps them to its own refusal vocabulary.
    """
    allowed = FILE_ARGUMENTS[call.name]
    unexpected = set(call.arguments) - allowed
    if unexpected:
        raise FileArgumentsInvalid(f"unexpected: {sorted(unexpected)}")

    if call.name == "file.read":
        path = _safe_path(call.arguments)
        return _helper(["read", path, str(READ_LIMIT_BYTES)])
    if call.name == "file.write":
        path = _safe_path(call.arguments)
        body = _write_body(call.arguments)
        return _helper(["write", path, str(WRITE_LIMIT_BYTES)], stdin=body)

    raw_path: Any = call.arguments.get("path", ".")
    path = "." if raw_path == "." else _safe_path({"path": raw_path})
    offset = _bounded_int(call.arguments, "offset", default=0, low=0, high=10_000_000)
    limit = _bounded_int(
        call.arguments, "limit", default=LIST_LIMIT_ENTRIES, low=1, high=LIST_LIMIT_ENTRIES
    )
    return _helper(["list", path, str(offset), str(limit)])


def changes_workspace(name: str) -> bool:
    """Whether a call may have altered `/workspace/data` (design §8)."""
    return name in ("file.write", "shell.exec")


def _safe_path(arguments: dict[str, Any]) -> str:
    raw: Any = arguments.get("path")
    if not isinstance(raw, str):
        raise FilePathRefused("path must be a string")
    try:
        return normalize_workspace_path(raw)
    except InvalidWorkspacePath as hostile:
        # The same generic refusal for every hostile shape: an attacker probing
        # the rule learns nothing about which rule fired.
        raise FilePathRefused(str(hostile)) from hostile


def _write_body(arguments: dict[str, Any]) -> bytes:
    content: Any = arguments.get("content")
    if not isinstance(content, str):
        raise FileArgumentsInvalid("content must be a string")
    body = content.encode("utf-8")
    if len(body) > WRITE_LIMIT_BYTES:
        # Refused rather than clamped: a clamp writes a different file than
        # the model asked for and says nothing about it.
        raise FileArgumentsInvalid(f"content of {len(body)} bytes is over the limit")
    return body


def _bounded_int(
    arguments: dict[str, Any], name: str, *, default: int, low: int, high: int
) -> int:
    value: Any = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise FileArgumentsInvalid(name)
    if not low <= value <= high:
        raise FileArgumentsInvalid(f"{name} outside [{low}, {high}]")
    return value


def _helper(arguments: list[str], *, stdin: bytes | None = None) -> SandboxCommand:
    return SandboxCommand(
        argv=[FILE_HELPER, "--root", DATA_ROOT, *arguments],
        cwd=DATA_ROOT,
        timeout_seconds=FILE_TOOL_TIMEOUT_SECONDS,
        # The read limit plus room for the helper's own stderr JSON.
        output_limit=READ_LIMIT_BYTES + 4_096,
        stdin=stdin,
    )
