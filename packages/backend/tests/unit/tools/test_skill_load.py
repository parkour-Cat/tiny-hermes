"""`skill.load`: the second tool the sandbox never sees.

It is the model's half of progressive loading (design §10.1). The platform puts
a one-line summary of every bound skill in the request; the document itself
costs nothing until the model asks for it by name. Like `platform.wait`, what
it does happens on the Run rather than in a container, so it is recognized by
name before `authorize` is ever reached.

This file covers only what the call itself says. Whether the named skill is one
*this Run* may read is the second authorization check, and it belongs to the
Worker, which is the only place that knows what the Run's Version bound.
"""

import pytest
from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.tools.domain.registry import (
    DEFAULT_SKILL_PATH,
    IMPLEMENTED_TOOLS,
    PLATFORM_TOOLS,
    RefusalReason,
    ToolRefused,
    authorize,
    schemas_for,
    skill_load_of,
)


def call(**arguments: object) -> ToolCallBlock:
    return ToolCallBlock(call_id="skill-1", name="skill.load", arguments=arguments)


def test_it_is_a_tool_an_agent_binds_like_any_other() -> None:
    assert "skill.load" in IMPLEMENTED_TOOLS
    assert "skill.load" in PLATFORM_TOOLS


def test_the_model_is_told_it_exists_when_the_agent_bound_it() -> None:
    names = [schema["function"]["name"] for schema in schemas_for(("skill.load",))]

    assert names == ["skill.load"]


def test_an_agent_that_did_not_bind_it_is_told_nothing_about_it() -> None:
    names = [schema["function"]["name"] for schema in schemas_for(("shell.exec",))]

    assert "skill.load" not in names


def test_it_is_never_handed_to_the_sandbox() -> None:
    with pytest.raises(ToolRefused) as refusal:
        authorize(bound=("skill.load",), call=call(skill="deploy"))

    assert refusal.value.reason is RefusalReason.UNKNOWN_TOOL


def test_a_skill_named_alone_reads_the_package_entry_point() -> None:
    """`SKILL.md` is the file every package has, so naming it is optional."""
    asked = skill_load_of(call(skill="deploy"))

    assert asked.skill == "deploy"
    assert asked.path == DEFAULT_SKILL_PATH


def test_another_file_in_the_package_can_be_named() -> None:
    asked = skill_load_of(call(skill="deploy", path="reference/rollback.md"))

    assert asked.path == "reference/rollback.md"


def test_surrounding_space_is_not_part_of_either_name() -> None:
    asked = skill_load_of(call(skill="  deploy  ", path=" SKILL.md "))

    assert (asked.skill, asked.path) == ("deploy", "SKILL.md")


@pytest.mark.parametrize("skill", ["", "   ", 7, None])
def test_a_call_that_names_no_skill_is_refused(skill: object) -> None:
    """The skill is the whole call. There is nothing to default it to."""
    with pytest.raises(ToolRefused) as refusal:
        skill_load_of(call(skill=skill))

    assert refusal.value.reason is RefusalReason.INVALID_ARGUMENTS


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../other/SKILL.md", "reference/../../SKILL.md", "", 3],
)
def test_a_path_that_leaves_the_package_is_refused(path: object) -> None:
    """A skill version's files are a closed set, keyed by the path inside it.

    An absolute path or a `..` segment is a model asking for something that
    package does not contain. Refused here rather than resolved and then
    looked up, so no lookup is ever performed with a path that meant to escape.
    """
    with pytest.raises(ToolRefused) as refusal:
        skill_load_of(call(skill="deploy", path=path))

    assert refusal.value.reason is RefusalReason.INVALID_ARGUMENTS


def test_a_file_named_with_two_dots_in_it_is_still_a_file() -> None:
    """`..` is refused as a whole path segment, not as a substring."""
    asked = skill_load_of(call(skill="deploy", path="notes..md"))

    assert asked.path == "notes..md"


def test_an_unknown_argument_is_refused() -> None:
    with pytest.raises(ToolRefused) as refusal:
        skill_load_of(call(skill="deploy", version="2"))

    assert refusal.value.reason is RefusalReason.INVALID_ARGUMENTS


def test_the_refusal_carries_the_call_id_it_answers() -> None:
    with pytest.raises(ToolRefused) as refusal:
        skill_load_of(call())

    assert refusal.value.call_id == "skill-1"
