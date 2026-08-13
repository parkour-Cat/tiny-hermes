"""The three file verbs, authorized twice and never handed to a shell.

Design §10: paths are relative to /workspace/data; absolute paths, `..`, and
NUL die before execution with the same generic refusal. Every command built
here execs the openat2 helper directly — no user string ever reaches bash.
"""

from typing import Any

import pytest
from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.tools.domain.files import (
    FILE_HELPER,
    LIST_LIMIT_ENTRIES,
    READ_LIMIT_BYTES,
    WRITE_LIMIT_BYTES,
)
from tiny_hermes.tools.domain.registry import (
    IMPLEMENTED_TOOLS,
    RefusalReason,
    ToolRefused,
    authorize,
    schemas_for,
)

BOUND = ("file.list", "file.read", "file.write", "shell.exec")


def _call(name: str, arguments: dict[str, Any]) -> ToolCallBlock:
    return ToolCallBlock(call_id="call-1", name=name, arguments=arguments)


def test_the_file_tools_are_implemented_and_advertised() -> None:
    assert set(BOUND) <= set(IMPLEMENTED_TOOLS)
    advertised = {schema["function"]["name"] for schema in schemas_for(BOUND)}
    assert advertised == set(BOUND)
    assert schemas_for(("file.read",)) != schemas_for(("file.write",))


@pytest.mark.parametrize("path", ["/etc/passwd", "../x", "a/../../x", "a\x00b", ""])
@pytest.mark.parametrize("name", ["file.read", "file.write", "file.list"])
def test_file_paths_outside_data_are_refused_before_execution(
    name: str, path: str
) -> None:
    arguments: dict[str, Any] = {"path": path}
    if name == "file.write":
        arguments["content"] = "x"
    with pytest.raises(ToolRefused) as refused:
        authorize(bound=BOUND, call=_call(name, arguments))
    assert refused.value.reason is RefusalReason.NOT_AUTHORIZED


def test_an_unbound_file_tool_is_refused_even_with_a_good_path() -> None:
    with pytest.raises(ToolRefused) as refused:
        authorize(bound=("shell.exec",), call=_call("file.read", {"path": "a.txt"}))
    assert refused.value.reason is RefusalReason.NOT_AUTHORIZED


def test_file_read_carries_its_byte_limit_and_execs_the_helper() -> None:
    authorized = authorize(bound=BOUND, call=_call("file.read", {"path": "notes/a.txt"}))

    assert authorized.command.argv == [
        FILE_HELPER,
        "--root",
        "/workspace/data",
        "read",
        "notes/a.txt",
        str(READ_LIMIT_BYTES),
    ]
    assert authorized.command.stdin is None
    assert not authorized.changes_workspace
    assert "bash" not in " ".join(authorized.command.argv)


def test_file_write_streams_its_body_on_stdin_and_changes_the_workspace() -> None:
    authorized = authorize(
        bound=BOUND, call=_call("file.write", {"path": "doc/x.md", "content": "hello"})
    )

    assert authorized.command.argv == [
        FILE_HELPER,
        "--root",
        "/workspace/data",
        "write",
        "doc/x.md",
        str(WRITE_LIMIT_BYTES),
    ]
    assert authorized.command.stdin == b"hello"
    assert authorized.changes_workspace


def test_file_write_refuses_bodies_over_sixteen_mib() -> None:
    heavy = "x" * (WRITE_LIMIT_BYTES + 1)
    with pytest.raises(ToolRefused) as refused:
        authorize(
            bound=BOUND, call=_call("file.write", {"path": "big.txt", "content": heavy})
        )
    assert refused.value.reason is RefusalReason.INVALID_ARGUMENTS


def test_file_write_measures_bytes_not_characters() -> None:
    """A multibyte body can exceed the limit while its character count does not."""
    sneaky = "é" * (WRITE_LIMIT_BYTES // 2 + 1)
    with pytest.raises(ToolRefused):
        authorize(
            bound=BOUND, call=_call("file.write", {"path": "b.txt", "content": sneaky})
        )


def test_file_list_is_paginated_not_recursive() -> None:
    authorized = authorize(
        bound=BOUND, call=_call("file.list", {"path": "notes", "offset": 40, "limit": 20})
    )
    assert authorized.command.argv[-3:] == ["notes", "40", "20"]
    assert not authorized.changes_workspace

    rootward = authorize(bound=BOUND, call=_call("file.list", {}))
    assert rootward.command.argv[-3:] == [".", "0", str(LIST_LIMIT_ENTRIES)]

    with pytest.raises(ToolRefused) as refused:
        authorize(
            bound=BOUND,
            call=_call("file.list", {"path": "notes", "limit": LIST_LIMIT_ENTRIES + 1}),
        )
    assert refused.value.reason is RefusalReason.INVALID_ARGUMENTS


def test_unexpected_arguments_are_refused_not_dropped() -> None:
    with pytest.raises(ToolRefused) as refused:
        authorize(
            bound=BOUND,
            call=_call("file.read", {"path": "a.txt", "follow_symlinks": True}),
        )
    assert refused.value.reason is RefusalReason.INVALID_ARGUMENTS


def test_shell_exec_still_authorizes_and_always_changes_the_workspace() -> None:
    authorized = authorize(bound=BOUND, call=_call("shell.exec", {"command": "ls"}))
    assert authorized.changes_workspace
    assert authorized.command.argv[0] == "/bin/bash"
