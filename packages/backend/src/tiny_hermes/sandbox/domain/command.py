"""What the Controller will run, in its own vocabulary rather than a tool's.

`argv`, not a shell string. A tool that wants a shell asks for one explicitly by
naming `/bin/bash` as argv[0]; the Controller never assembles a command line out
of parts, because a Controller that did would own the quoting bug for every tool
that ever exists.
"""

from dataclasses import dataclass
from typing import Protocol

#: The only directories a command may start in. Both are mounts this Run owns.
ALLOWED_WORKING_DIRECTORIES = ("/workspace/data", "/workspace/cache")


@dataclass(frozen=True)
class SandboxCommand:
    argv: list[str]
    cwd: str
    timeout_seconds: int
    #: Bytes of output kept. Beyond it the result is marked truncated, which is
    #: a fact the model is told rather than a shorter answer it cannot tell from
    #: a complete one.
    output_limit: int
    #: Bytes fed to the process before its stdin closes. `file.write` bodies
    #: travel here so no content ever rides a command line or a shell.
    stdin: bytes | None = None


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    output: str
    truncated: bool
    timed_out: bool


@dataclass(frozen=True)
class ScannedEntry:
    """One tree member as the scan saw it, judged by nobody yet.

    The scanner reports symlinks, devices, FIFOs, and sockets rather than
    skipping them (design §8): a checkpoint that silently dropped an entry
    would commit a revision that restores to something other than what ran.
    Refusing is the caller's decision, made on a complete report.
    """

    path: str
    entry_type: str
    mode: int
    size: int
    sha256: str | None


class OutputSink(Protocol):
    """Where streamed command output goes, up to the sink's own ceiling.

    The preview/artifact split is the consumer's business; the engine's whole
    contract is to deliver at most ``artifact_limit`` bytes and keep draining
    the child beyond them, so nothing ever blocks on a full pipe.
    """

    @property
    def artifact_limit(self) -> int: ...

    async def deliver(self, chunk: bytes) -> None: ...


@dataclass(frozen=True)
class StreamedResult:
    exit_code: int
    timed_out: bool
    #: Everything the child wrote, including what was drained and discarded.
    observed_bytes: int
    #: What actually reached the sink.
    delivered_bytes: int
    truncated: bool
