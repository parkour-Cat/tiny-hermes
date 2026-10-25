"""`site.publish` as the model is told it and as its arguments are read (§1.26, v2.26)."""

import pytest
from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.tools.domain.registry import (
    IMPLEMENTED_TOOLS,
    PLATFORM_TOOLS,
    ToolRefused,
    schemas_for,
    site_publish_of,
)


def _call(**arguments: object) -> ToolCallBlock:
    return ToolCallBlock(call_id="c1", name="site.publish", arguments=dict(arguments))


def test_it_is_a_platform_tool_an_agent_can_bind() -> None:
    """Answered by the platform: there is no command to run in a sandbox."""
    assert "site.publish" in IMPLEMENTED_TOOLS
    assert "site.publish" in PLATFORM_TOOLS


def test_a_bound_agent_is_told_to_publish_in_a_round_of_its_own() -> None:
    """Files written in the same round are not committed yet, so a publish
    beside them would serve the snapshot before them."""
    [schema] = schemas_for(("site.publish",))

    function = schema["function"]
    assert function["name"] == "site.publish"
    assert "own round" in function["description"]
    assert set(function["parameters"]["properties"]) == {"path", "title"}


def test_the_directory_defaults_to_site() -> None:
    assert site_publish_of(_call()) == ("site", None)
    assert site_publish_of(_call(path="www", title="Ledger")) == ("www", "Ledger")


@pytest.mark.parametrize(
    "arguments",
    [{"path": 3}, {"path": ""}, {"title": 7}, {"title": "x" * 101}, {"url": "http://x"}],
)
def test_arguments_of_the_wrong_shape_are_refused(arguments: dict[str, object]) -> None:
    with pytest.raises(ToolRefused):
        site_publish_of(_call(**arguments))
