"""Tool calls, in both directions, over recorded bodies.

Phase 3A normalized `finish_reason: "tool_calls"` into a failed round with
`tool_use_not_supported`, because no tool was bound and a model asking for one
had left the contract. A tool is bound now, so that becomes a parse.

The outbound direction matters as much and is easier to get wrong quietly:
`CanonicalMessage.text` still exists and still compiles, so an adapter that
walked it instead of `blocks` would silently send a conversation with every
tool call and result missing — and the model would answer as though it had
never acted.
"""

import json
from typing import Any

import pytest
from tiny_hermes.agents.domain.models import DeterministicModelPolicy
from tiny_hermes.model_catalog.domain.models import ModelEndpointSpec
from tiny_hermes.runs.domain.models import (
    CanonicalMessage,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from tiny_hermes.runs.infrastructure.openai_model import build_payload, normalize
from tiny_hermes.runs.ports.model import ModelRequest, StopReason

SPEC = ModelEndpointSpec.model_validate(
    {
        "name": "stand-in",
        "kind": "openai_compatible",
        "base_url": "https://models.example.com/v1",
        "model": "stand-in-large",
        "context_window": 8_192,
        "max_output_tokens": 512,
        "usage_quality": "provider",
        "credential_ref": "TINY_HERMES_TEST_MODEL_KEY",
    }
)


def answer(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "shell.exec",
                                "arguments": '{"command": "ls", "cwd": "/workspace/data"}',
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }
    body.update(overrides)
    return body


def sent(*messages: CanonicalMessage, tools: list[dict[str, Any]] | None = None) -> Any:
    request = ModelRequest(
        policy=DeterministicModelPolicy(),
        personality="You are careful.",
        messages=messages,
        round_index=1,
    )
    return build_payload(SPEC, request, tools=tools or [])


# -- inbound ---------------------------------------------------------------


def test_a_tool_call_is_parsed_rather_than_refused() -> None:
    response = normalize(answer())

    assert response.stop_reason is StopReason.TOOL_CALL
    assert response.tool_calls == (
        ToolCallBlock(
            call_id="call_1",
            name="shell.exec",
            arguments={"command": "ls", "cwd": "/workspace/data"},
        ),
    )


def test_text_alongside_a_call_is_kept() -> None:
    """A model that explains itself before acting said something worth storing."""
    body = answer()
    body["choices"][0]["message"]["content"] = "Listing the directory."
    response = normalize(body)

    assert response.text == "Listing the directory."
    assert len(response.tool_calls) == 1


def test_arguments_are_decoded_once_at_the_edge() -> None:
    """The provider sends a JSON string. Everything inward gets an object.

    Carrying the string would mean every reader decoded it, and each one could
    decide differently what a malformed one means.
    """
    call = normalize(answer()).tool_calls[0]
    assert isinstance(call.arguments, dict)
    assert call.arguments["command"] == "ls"


def test_malformed_arguments_fail_the_round_with_a_name() -> None:
    body = answer()
    body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{not json"
    response = normalize(body)

    assert response.stop_reason is StopReason.FAILED
    assert response.failure == "malformed_tool_arguments"


def test_a_call_with_no_name_fails_rather_than_being_dispatched() -> None:
    body = answer()
    body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = ""
    response = normalize(body)

    assert response.stop_reason is StopReason.FAILED
    assert response.failure == "malformed_tool_call"


def test_a_finish_reason_of_tool_calls_with_no_calls_fails() -> None:
    body = answer()
    body["choices"][0]["message"]["tool_calls"] = []
    response = normalize(body)

    assert response.stop_reason is StopReason.FAILED
    assert response.failure == "malformed_tool_call"


def test_several_calls_in_one_round_are_all_parsed() -> None:
    """A model may ask for two things at once, and dropping the second would
    make the platform answer a question nobody asked."""
    body = answer()
    body["choices"][0]["message"]["tool_calls"].append(
        {
            "id": "call_2",
            "type": "function",
            "function": {"name": "shell.exec", "arguments": '{"command": "pwd"}'},
        }
    )
    response = normalize(body)

    assert [call.call_id for call in response.tool_calls] == ["call_1", "call_2"]


# -- outbound --------------------------------------------------------------


def test_a_transcript_with_a_call_and_a_result_is_sent_whole() -> None:
    """The test that `.text` would have let pass while dropping everything."""
    payload = sent(
        CanonicalMessage(role="user", blocks=(TextBlock(text="list the files"),)),
        CanonicalMessage(
            role="assistant",
            blocks=(
                TextBlock(text="Listing."),
                ToolCallBlock(call_id="call_1", name="shell.exec", arguments={"command": "ls"}),
            ),
        ),
        CanonicalMessage(
            role="tool",
            blocks=(
                ToolResultBlock(call_id="call_1", output="a.txt\n", exit_code=0, failed=False),
            ),
        ),
    )
    messages = payload["messages"][2:]  # after the two system messages

    assert messages[0] == {"role": "user", "content": "list the files"}
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "Listing."
    # `shell__exec`, not `shell.exec`: an OpenAI-compatible endpoint refuses a
    # function name with a dot in it. This assertion used to pin the dotted
    # name, which is why the refusal was never noticed — the test agreed with
    # the adapter and both disagreed with every real endpoint.
    assert messages[1]["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "shell__exec", "arguments": json.dumps({"command": "ls"})},
        }
    ]
    assert messages[2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "a.txt\n",
    }


def test_an_assistant_message_that_is_only_a_call_sends_null_content() -> None:
    """What the API expects, and what a model that acted without speaking did."""
    payload = sent(
        CanonicalMessage(
            role="assistant",
            blocks=(ToolCallBlock(call_id="c", name="shell.exec", arguments={}),),
        )
    )
    assert payload["messages"][2]["content"] is None


def test_two_results_become_two_messages() -> None:
    """One per call id, because the API ties each result to the call it answers."""
    payload = sent(
        CanonicalMessage(
            role="tool",
            blocks=(
                ToolResultBlock(call_id="a", output="1", exit_code=0, failed=False),
                ToolResultBlock(call_id="b", output="2", exit_code=0, failed=False),
            ),
        )
    )
    results = payload["messages"][2:]
    assert [entry["tool_call_id"] for entry in results] == ["a", "b"]


def test_no_tools_are_advertised_when_the_agent_binds_none() -> None:
    """§10.2's first step. A model told about no tool cannot correctly ask for one."""
    assert "tools" not in sent(
        CanonicalMessage(role="user", blocks=(TextBlock(text="hi"),))
    )


def test_a_bound_tool_is_advertised() -> None:
    schema: dict[str, Any] = {
        "type": "function",
        "function": {"name": "shell.exec", "parameters": {}},
    }
    payload = sent(
        CanonicalMessage(role="user", blocks=(TextBlock(text="hi"),)), tools=[schema]
    )
    # Advertised under the wire name. Everything else about the schema is
    # passed through untouched, including `parameters`.
    assert payload["tools"] == [
        {"type": "function", "function": {"name": "shell__exec", "parameters": {}}}
    ]
    # And the caller's own dict was not mutated — the registry hands out
    # module-level constants, so renaming in place would rename them for
    # every later request, including the deterministic provider's.
    assert schema["function"]["name"] == "shell.exec"


@pytest.mark.parametrize("failed", [True, False])
def test_a_failed_result_still_reaches_the_model(failed: bool) -> None:
    """A tool that was refused is information the model needs, not an error to
    swallow — otherwise it retries the same refused call forever."""
    payload = sent(
        CanonicalMessage(
            role="tool",
            blocks=(
                ToolResultBlock(
                    call_id="c", output="not authorized", exit_code=1, failed=failed
                ),
            ),
        )
    )
    assert payload["messages"][2]["content"] == "not authorized"


# --- the wire name, which is not this platform's name --------------------

WIRE_PATTERN = __import__("re").compile(r"^[a-zA-Z0-9_-]+$")


def test_every_advertised_tool_name_is_one_the_wire_accepts() -> None:
    """OpenAI-compatible endpoints constrain a function name to
    `^[a-zA-Z0-9_-]+$`. Every tool this platform implements has a dot in it —
    `shell.exec`, `file.read` — so every request advertising one was refused:

        Invalid 'tools[0].function.name': string does not match pattern.

    DeepSeek answered `400` to every call from an Agent with tools bound.
    Nothing caught it because the whole suite runs against the deterministic
    provider and no e2e walk ever pointed at a real OpenAI-compatible
    endpoint — the request was well-formed by our lights and rejected by
    theirs.
    """
    from tiny_hermes.tools.domain.registry import IMPLEMENTED_TOOLS, schemas_for

    # Through `build_payload`, not the registry directly: the registry keeps
    # this platform's own names — dots and all — and the rename belongs to
    # the OpenAI adapter, which is the only place the constraint exists.
    payload = sent(
        CanonicalMessage(role="user", blocks=(TextBlock(text="hi"),)),
        tools=schemas_for(IMPLEMENTED_TOOLS),
    )

    advertised = [entry["function"]["name"] for entry in payload["tools"]]
    assert len(advertised) == len(IMPLEMENTED_TOOLS)
    for name in advertised:
        assert WIRE_PATTERN.fullmatch(name), name


def test_a_tool_call_comes_back_under_the_platform_s_own_name() -> None:
    """The other half, and the half that fails silently if forgotten.

    Renaming on the way out without renaming on the way back leaves the
    model asking for `file_read`, a name no part of this platform
    dispatches — authorisation would refuse a tool the Agent genuinely
    bound.
    """
    body = answer()
    # `__`, not `_`: a single underscore could not be reversed, because a
    # platform name may legitimately contain one.
    body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = "shell__exec"

    response = normalize(body)

    assert response.stop_reason is StopReason.TOOL_CALL
    assert [call.name for call in response.tool_calls] == ["shell.exec"]


def test_a_replayed_tool_call_goes_out_under_the_wire_name() -> None:
    """History is sent back to the model on the next round. A call replayed
    under the dotted name is the same 400, one turn later."""
    payload = sent(
        CanonicalMessage(
            role="assistant",
            blocks=(
                ToolCallBlock(call_id="call_1", name="shell.exec", arguments={"command": "ls"}),
            ),
        )
    )

    replayed = [m for m in payload["messages"] if m.get("tool_calls")]
    assert replayed, "the tool call was not replayed at all"
    assert replayed[0]["tool_calls"][0]["function"]["name"] == "shell__exec"
