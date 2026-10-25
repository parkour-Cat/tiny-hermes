"""`file.send`'s shape (§1.27, v2.27): one saved file, answered by the platform."""

import pytest
from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.tools.domain.registry import (
    IMPLEMENTED_TOOLS,
    PLATFORM_TOOLS,
    ToolRefused,
    file_send_of,
    schemas_for,
)


def _call(**arguments: object) -> ToolCallBlock:
    return ToolCallBlock(call_id="s1", name="file.send", arguments=dict(arguments))


def test_it_is_a_tool_the_platform_answers() -> None:
    """It reads the saved snapshot and talks to Feishu; nothing runs in the
    sandbox, and a container held for it would be held for nothing."""
    assert "file.send" in IMPLEMENTED_TOOLS
    assert "file.send" in PLATFORM_TOOLS


def test_its_schema_says_to_send_in_a_round_of_its_own() -> None:
    [schema] = schemas_for(("file.send",))

    assert schema["function"]["name"] == "file.send"
    assert "own round" in schema["function"]["description"]


@pytest.mark.parametrize(
    ("given", "path"),
    [("out/周报.xlsx", "out/周报.xlsx"), ("  report.pdf ", "report.pdf"), ("./a.docx", "a.docx")],
)
def test_a_path_is_read_as_given(given: str, path: str) -> None:
    assert file_send_of(_call(path=given)) == path


@pytest.mark.parametrize(
    "arguments",
    [{}, {"path": ""}, {"path": 3}, {"path": "a.xlsx", "to": "boss"}, {"path": "../etc/x"}],
)
def test_anything_else_is_refused(arguments: dict[str, object]) -> None:
    with pytest.raises(ToolRefused):
        file_send_of(_call(**arguments))
