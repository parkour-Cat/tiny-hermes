"""`platform.wait`: the first tool the sandbox never sees.

Design §4.6 needs a producer for `waiting_external` or the Scheduler's wait scan
stays a scan with nothing to find. A model asks the platform for things by
calling a tool — that is the only channel it has — so waiting is a tool, bound
like any other. An Agent that does not bind it cannot be made to wait.

What makes this one different is that it resolves in the platform rather than in
the sandbox. `authorize` answers with a `SandboxCommand`, and there is no
command here to answer with, so the split happens before authorization: a
platform tool is recognized by name and parsed on its own.
"""

import pytest
from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.tools.domain.registry import (
    IMPLEMENTED_TOOLS,
    MAX_WAIT_SECONDS,
    PLATFORM_TOOLS,
    RefusalReason,
    ToolRefused,
    authorize,
    schemas_for,
    wait_seconds_of,
)


def call(**arguments: object) -> ToolCallBlock:
    return ToolCallBlock(call_id="wait-1", name="platform.wait", arguments=arguments)


def test_it_is_a_tool_an_agent_binds_like_any_other() -> None:
    assert "platform.wait" in IMPLEMENTED_TOOLS
    assert "platform.wait" in PLATFORM_TOOLS


def test_the_model_is_told_it_exists_when_the_agent_bound_it() -> None:
    names = [schema["function"]["name"] for schema in schemas_for(("platform.wait",))]

    assert names == ["platform.wait"]


def test_an_agent_that_did_not_bind_it_is_told_nothing_about_it() -> None:
    assert schemas_for(("shell.exec",)) == schemas_for(("shell.exec",))
    names = [schema["function"]["name"] for schema in schemas_for(("shell.exec",))]
    assert "platform.wait" not in names


def test_a_duration_comes_back_as_a_number_of_seconds() -> None:
    assert wait_seconds_of(call(seconds=300)) == 300


def test_it_is_never_handed_to_the_sandbox() -> None:
    """`authorize` builds commands, and this call has none to build.

    Refused there rather than given a harmless no-op command, because a no-op
    that reached the Controller would be a real container doing nothing while
    the Run is supposed to be holding no container at all.
    """
    with pytest.raises(ToolRefused) as refusal:
        authorize(bound=("platform.wait",), call=call(seconds=300))

    assert refusal.value.reason is RefusalReason.UNKNOWN_TOOL


@pytest.mark.parametrize("seconds", [0, -1, "300", 1.5, None])
def test_a_duration_that_is_not_a_positive_whole_number_is_refused(
    seconds: object,
) -> None:
    """Including the string "300".

    A model that sends a string has not said 300 seconds; it has sent something
    that looks like it. Coercing here would make the platform guess at the one
    number that decides how long a Run stops being worked on.
    """
    with pytest.raises(ToolRefused) as refusal:
        wait_seconds_of(call(seconds=seconds))

    assert refusal.value.reason is RefusalReason.INVALID_ARGUMENTS


def test_a_missing_duration_is_refused_rather_than_defaulted() -> None:
    """There is no sensible default. How long to wait is the whole call."""
    with pytest.raises(ToolRefused) as refusal:
        wait_seconds_of(call())

    assert refusal.value.reason is RefusalReason.INVALID_ARGUMENTS


def test_an_unknown_argument_is_refused() -> None:
    with pytest.raises(ToolRefused):
        wait_seconds_of(call(seconds=300, until="tomorrow"))


def test_a_wait_longer_than_the_ceiling_is_refused() -> None:
    """A Run that asked to sleep for a year would sit in `waiting_external`
    holding a Session's head for a year. The ceiling is the platform's answer,
    not the model's."""
    with pytest.raises(ToolRefused) as refusal:
        wait_seconds_of(call(seconds=MAX_WAIT_SECONDS + 1))

    assert refusal.value.reason is RefusalReason.INVALID_ARGUMENTS


def test_the_ceiling_itself_is_allowed() -> None:
    assert wait_seconds_of(call(seconds=MAX_WAIT_SECONDS)) == MAX_WAIT_SECONDS


def test_the_refusal_carries_the_call_id_it_answers() -> None:
    """Same rule as every other refusal: a model left without a result for a
    call it made will either retry it or invent what it returned."""
    with pytest.raises(ToolRefused) as refusal:
        wait_seconds_of(call(seconds=0))

    assert refusal.value.call_id == "wait-1"
