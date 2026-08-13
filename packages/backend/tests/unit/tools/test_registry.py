"""Two-step authorization, and the two steps fail differently.

Technical design §10.2. The first step decides what the model is *told about*;
the second decides what actually runs, against the real arguments. They are
tested apart because only the second one matters when something goes wrong: a
model can ask for anything regardless of what it was told, so a platform that
relied on the schema list to keep a tool unreachable would be relying on the
model's good manners.
"""

from typing import Any

import pytest
from tiny_hermes.agents.domain.models import AgentSpec, normalize_agent_spec
from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.tools.domain.registry import (
    AuthorizedCall,
    RefusalReason,
    ToolRefused,
    authorize,
    schemas_for,
)

from ..agents.test_agent_models import valid_spec

#: The hash of a spec that binds no tools, from before the field could hold
#: one. Pinned because widening `tools` had to be either free or a recorded
#: `schema_version` bump, and this is the evidence it was free.
UNBOUND_HASH = "4fcf412e8da2d827a601dbc4390d072a21bef593c080a2076041edb4ffb6deaf"


def call(**overrides: Any) -> ToolCallBlock:
    fields: dict[str, Any] = {
        "call_id": "c1",
        "name": "shell.exec",
        "arguments": {"command": "ls -la", "cwd": "/workspace/data"},
    }
    fields.update(overrides)
    return ToolCallBlock(**fields)


# -- the schema change was free --------------------------------------------


def test_an_agent_that_binds_no_tools_hashes_to_what_it_always_did() -> None:
    """`tools` already serialized to `[]`, and a default of `()` still does.

    So no published version is disturbed and `schema_version` stays 1 — the
    same argument the model policy union made, checked rather than assumed.
    """
    spec = AgentSpec.model_validate(valid_spec())
    document, content_hash = normalize_agent_spec(spec)
    assert document["tools"] == []
    assert content_hash == UNBOUND_HASH


def test_binding_a_tool_changes_the_hash() -> None:
    bound = AgentSpec.model_validate({**valid_spec(), "tools": ["shell.exec"]})
    assert normalize_agent_spec(bound)[1] != UNBOUND_HASH


def test_a_tool_the_platform_does_not_implement_is_refused_at_publish() -> None:
    """An AgentVersion naming a tool nobody wrote would be a Run that fails on
    its first call, discovered by whoever submitted it rather than by its author."""
    with pytest.raises(ValueError, match="unknown tool"):
        AgentSpec.model_validate({**valid_spec(), "tools": ["shell.rm_rf"]})


def test_the_same_tool_twice_is_refused() -> None:
    with pytest.raises(ValueError):
        AgentSpec.model_validate({**valid_spec(), "tools": ["shell.exec", "shell.exec"]})


# -- step one: what the model is told about --------------------------------


def test_an_agent_that_binds_nothing_advertises_nothing() -> None:
    assert schemas_for(()) == []


def test_a_bound_tool_is_advertised_with_a_schema() -> None:
    schemas = schemas_for(("shell.exec",))
    assert [entry["function"]["name"] for entry in schemas] == ["shell.exec"]
    assert "command" in schemas[0]["function"]["parameters"]["properties"]


def test_the_schema_says_there_is_no_network() -> None:
    """The model plans against what it is told. A description that omitted this
    would produce Agents that keep trying to curl things and reporting failure."""
    described = schemas_for(("shell.exec",))[0]["function"]["description"].lower()
    assert "no network" in described


# -- step two: what actually runs ------------------------------------------


def test_an_unbound_tool_is_refused_even_when_the_call_is_perfect() -> None:
    """The step that matters. A model can ask for anything.

    Nothing about this call is wrong except that this Agent never bound the
    tool, and the schema list it was given is not a control — only this is.
    """
    with pytest.raises(ToolRefused) as refused:
        authorize(bound=(), call=call())
    assert refused.value.reason is RefusalReason.NOT_AUTHORIZED


def test_a_tool_nobody_implements_is_refused() -> None:
    with pytest.raises(ToolRefused) as refused:
        authorize(bound=("shell.exec",), call=call(name="shell.rm_rf"))
    assert refused.value.reason is RefusalReason.UNKNOWN_TOOL


def test_a_bound_tool_with_good_arguments_becomes_a_command() -> None:
    authorized = authorize(bound=("shell.exec",), call=call())
    assert isinstance(authorized, AuthorizedCall)
    assert authorized.command.argv == ["/bin/bash", "-lc", "ls -la"]
    assert authorized.command.cwd == "/workspace/data"


@pytest.mark.parametrize(
    "cwd",
    [
        "/etc",
        "/",
        "/workspace",
        "/workspace/data/../../etc",
        "workspace/data",
        "/workspace/datax",
    ],
)
def test_a_working_directory_outside_the_workspace_is_refused(cwd: str) -> None:
    """Including via `..`, which is the one somebody will actually try."""
    with pytest.raises(ToolRefused) as refused:
        authorize(bound=("shell.exec",), call=call(arguments={"command": "ls", "cwd": cwd}))
    assert refused.value.reason is RefusalReason.WORKING_DIRECTORY_NOT_ALLOWED


@pytest.mark.parametrize("cwd", ["/workspace/data", "/workspace/data/src", "/workspace/cache"])
def test_a_working_directory_inside_the_workspace_is_allowed(cwd: str) -> None:
    authorize(bound=("shell.exec",), call=call(arguments={"command": "ls", "cwd": cwd}))


def test_the_working_directory_defaults_to_the_data_mount() -> None:
    authorized = authorize(bound=("shell.exec",), call=call(arguments={"command": "ls"}))
    assert authorized.command.cwd == "/workspace/data"


def test_a_timeout_above_the_ceiling_is_refused_rather_than_clamped() -> None:
    """Clamping would run a different command than the model asked for and say
    nothing, which is the same objection as the output ceiling at publish."""
    with pytest.raises(ToolRefused) as refused:
        authorize(
            bound=("shell.exec",),
            call=call(arguments={"command": "ls", "timeout_seconds": 100_000}),
        )
    assert refused.value.reason is RefusalReason.TIMEOUT_TOO_LONG


def test_a_shorter_timeout_is_honoured() -> None:
    authorized = authorize(
        bound=("shell.exec",), call=call(arguments={"command": "ls", "timeout_seconds": 5})
    )
    assert authorized.command.timeout_seconds == 5


@pytest.mark.parametrize("arguments", [{}, {"command": ""}, {"command": "   "}, {"command": 7}])
def test_a_call_without_a_usable_command_is_refused(arguments: dict[str, Any]) -> None:
    with pytest.raises(ToolRefused) as refused:
        authorize(bound=("shell.exec",), call=call(arguments=arguments))
    assert refused.value.reason is RefusalReason.INVALID_ARGUMENTS


def test_an_unexpected_argument_is_refused_rather_than_ignored() -> None:
    """A model that asked for something the platform silently dropped believes
    it got what it asked for."""
    with pytest.raises(ToolRefused) as refused:
        authorize(
            bound=("shell.exec",),
            call=call(arguments={"command": "ls", "user": "root", "network": True}),
        )
    assert refused.value.reason is RefusalReason.INVALID_ARGUMENTS


def test_the_command_is_never_split_by_the_platform() -> None:
    """`/bin/bash -lc <string>` hands the whole line to a shell that owns the
    quoting. A platform that split it would own every quoting bug of every
    command any Agent ever writes.
    """
    authorized = authorize(
        bound=("shell.exec",),
        call=call(arguments={"command": "echo 'a b'  |  wc -l"}),
    )
    assert authorized.command.argv == ["/bin/bash", "-lc", "echo 'a b'  |  wc -l"]


def test_a_refusal_carries_the_call_it_answers() -> None:
    """So the loop can send a result back rather than leaving the model waiting
    on a call that never gets an answer."""
    with pytest.raises(ToolRefused) as refused:
        authorize(bound=(), call=call(call_id="c9"))
    assert refused.value.call_id == "c9"
