"""A thinking model's own reasoning, carried back to it.

DeepSeek's thinking mode returns `reasoning_content` alongside the answer
and **requires it back** on the next request. This platform dropped it, so
a multi-turn conversation worked until the model first produced reasoning
and then failed on every following round with:

    400 The `reasoning_content` in the thinking mode must be passed back
        to the API.

Found on a live tenant, and only because a diagnostic added earlier the
same day logs the endpoint's refusal body — without it this is an opaque
`endpoint_status:400` on a Run whose transcript looks perfectly healthy.

The other half of these tests is that nothing is invented for an endpoint
that never sent any: a field this platform made up is a request a
non-thinking endpoint is entitled to refuse.
"""

import json
from typing import Any

from tiny_hermes.agents.domain.models import DeterministicModelPolicy
from tiny_hermes.model_catalog.domain.models import ModelEndpointSpec
from tiny_hermes.runs.domain.models import (
    CanonicalMessage,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
)
from tiny_hermes.runs.infrastructure.openai_model import build_payload, normalize
from tiny_hermes.runs.ports.model import ModelRequest, StopReason


def _answer(**message: Any) -> dict[str, Any]:
    return {
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", **message}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _spec() -> ModelEndpointSpec:
    return ModelEndpointSpec(
        base_url="https://example.invalid/v1",
        model="thinker",
        credential_ref="KEY",
    )


def _payload(*messages: CanonicalMessage) -> dict[str, Any]:
    return build_payload(
        _spec(),
        ModelRequest(
            messages=tuple(messages),
            policy=DeterministicModelPolicy(temperature=0.0, top_p=1.0, seed=1),
            tools=(),
        ),
    )


def test_reasoning_is_read_off_a_thinking_answer() -> None:
    response = normalize(_answer(content="十二个。", reasoning_content="先数一遍…"))

    assert response.stop_reason is StopReason.COMPLETED
    assert response.reasoning == "先数一遍…"


def test_an_answer_without_reasoning_reports_none() -> None:
    """Not an empty string. `None` says the endpoint sent none, and empty
    would be indistinguishable from a model that thought about nothing —
    which decides whether the field is sent back at all."""
    response = normalize(_answer(content="十二个。"))

    assert response.reasoning is None


def test_reasoning_survives_a_tool_round() -> None:
    """The round that most needs it. A thinking model reasons, calls a tool,
    and the *next* request replays this turn — which is exactly where the
    400 was raised."""
    body = {
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "得先看看目录",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "file__read", "arguments": "{}"},
                        }
                    ],
                },
            }
        ]
    }

    response = normalize(body)

    assert response.stop_reason is StopReason.TOOL_CALL
    assert response.reasoning == "得先看看目录"


def test_a_replayed_turn_carries_its_reasoning_back() -> None:
    """The fix, stated as the API sees it."""
    turn = CanonicalMessage(
        "assistant",
        (ReasoningBlock(text="先数一遍…"), TextBlock(text="十二个。")),
    )

    sent = _payload(turn)["messages"]

    assistant = [m for m in sent if m["role"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["reasoning_content"] == "先数一遍…"
    assert assistant[0]["content"] == "十二个。"


def test_a_replayed_tool_turn_carries_its_reasoning_back() -> None:
    turn = CanonicalMessage(
        "assistant",
        (
            ReasoningBlock(text="得先看看目录"),
            ToolCallBlock(call_id="call_1", name="file.read", arguments={}),
        ),
    )

    sent = _payload(turn)["messages"]

    assistant = [m for m in sent if m["role"] == "assistant"]
    assert assistant[0]["reasoning_content"] == "得先看看目录"
    assert assistant[0]["tool_calls"][0]["id"] == "call_1"


def test_a_turn_with_no_reasoning_sends_no_such_field() -> None:
    """Nothing invented for an endpoint that never sent any. A field this
    platform made up is one a non-thinking endpoint may refuse, which would
    trade a bug affecting thinking models for a bug affecting every other
    kind."""
    turn = CanonicalMessage("assistant", (TextBlock(text="十二个。"),))

    sent = _payload(turn)["messages"]

    assistant = [m for m in sent if m["role"] == "assistant"]
    assert "reasoning_content" not in assistant[0]


def test_reasoning_is_not_part_of_what_the_person_said() -> None:
    """`text` is what a transcript shows and what the Feishu reply quotes.
    A model's private reasoning appearing there would put its scratch work
    in front of an end user — and §19.1 keeps internal state off that
    surface."""
    turn = CanonicalMessage(
        "assistant",
        (ReasoningBlock(text="先数一遍…"), TextBlock(text="十二个。")),
    )

    assert turn.text == "十二个。"


def test_a_stored_reasoning_block_reads_back() -> None:
    """It has to survive the database, or the replay has nothing to send."""
    from tiny_hermes.runs.domain.models import message_from_document

    turn = CanonicalMessage(
        "assistant",
        (ReasoningBlock(text="先数一遍…"), TextBlock(text="十二个。")),
    )

    restored = message_from_document(json.loads(json.dumps(turn.document())))

    assert restored.blocks == turn.blocks
