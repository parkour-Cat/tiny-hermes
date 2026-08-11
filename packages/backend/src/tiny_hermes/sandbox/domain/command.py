"""What the Controller will run, in its own vocabulary rather than a tool's.

`argv`, not a shell string. A tool that wants a shell asks for one explicitly by
naming `/bin/bash` as argv[0]; the Controller never assembles a command line out
of parts, because a Controller that did would own the quoting bug for every tool
that ever exists.
"""

from dataclasses import dataclass

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


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    output: str
    truncated: bool
    timed_out: bool
