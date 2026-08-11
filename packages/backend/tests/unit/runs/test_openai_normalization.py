"""Turning one endpoint's answer into one platform round.

Pure, over recorded bodies. Every failure here is `replay_safe`: the request
produced no text this platform kept and no side effect it knows of, so the Run
may be retried — and that is what decides whether the console offers 重试.
"""

from typing import Any

import pytest
from tiny_hermes.runs.infrastructure.openai_model import normalize
from tiny_hermes.runs.ports.model import StopReason, UsageQuality


def body(**overrides: Any) -> dict[str, Any]:
    answer: dict[str, Any] = {
        "id": "chatcmpl-1",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "the answer"},
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }
    answer.update(overrides)
    return answer


def choice(**overrides: Any) -> list[dict[str, Any]]:
    entry: dict[str, Any] = {
        "index": 0,
        "finish_reason": "stop",
        "message": {"role": "assistant", "content": "the answer"},
    }
    entry.update(overrides)
    return [entry]


def test_a_finished_answer_completes_the_round() -> None:
    response = normalize(body())
    assert response.stop_reason is StopReason.COMPLETED
    assert response.text == "the answer"
    assert response.input_tokens == 11
    assert response.output_tokens == 7
    assert response.usage_quality is UsageQuality.PROVIDER


def test_a_truncated_answer_fails_rather_than_passing_as_finished() -> None:
    """`length` means the model was cut off. Treating it as an answer would
    hand the caller half a thought and call it done."""
    response = normalize(body(choices=choice(finish_reason="length")))
    assert response.stop_reason is StopReason.FAILED
    assert response.failure == "max_output_reached"


def test_a_tool_call_fails_because_no_tool_is_bound() -> None:
    """Phase 3A binds no tools and tells the model about none.

    A model that asks for one has left the contract, and the honest answer is
    to stop — not to invent a result, and not to quietly drop the request and
    return whatever text came with it.
    """
    response = normalize(
        body(
            choices=choice(
                finish_reason="tool_calls",
                message={"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            )
        )
    )
    assert response.stop_reason is StopReason.FAILED
    assert response.failure == "tool_use_not_supported"


def test_an_unrecognized_finish_reason_fails_rather_than_being_assumed_good() -> None:
    response = normalize(body(choices=choice(finish_reason="content_filter")))
    assert response.stop_reason is StopReason.FAILED
    assert response.failure == "unsupported_stop_reason"


def test_a_finished_answer_with_no_text_is_a_failure() -> None:
    """An empty completed round would look like an Agent that answered nothing."""
    response = normalize(body(choices=choice(message={"role": "assistant", "content": ""})))
    assert response.stop_reason is StopReason.FAILED
    assert response.failure == "empty_response"


def test_no_choices_at_all_is_a_failure() -> None:
    response = normalize(body(choices=[]))
    assert response.stop_reason is StopReason.FAILED
    assert response.failure == "empty_response"


def test_absent_usage_is_reported_as_unknown_rather_than_zero() -> None:
    answer = body()
    del answer["usage"]
    response = normalize(answer)
    assert response.usage_quality is UsageQuality.UNAVAILABLE
    assert response.input_tokens is None
    assert response.output_tokens is None
    assert response.billable_tokens == 0


def test_partial_usage_is_still_unknown() -> None:
    """One of the two counts is not a usage report, it is half of one."""
    response = normalize(body(usage={"prompt_tokens": 11}))
    assert response.usage_quality is UsageQuality.UNAVAILABLE


def test_a_non_numeric_usage_is_not_trusted() -> None:
    response = normalize(body(usage={"prompt_tokens": "eleven", "completion_tokens": 7}))
    assert response.usage_quality is UsageQuality.UNAVAILABLE


NOT_A_COMPLETION: list[dict[str, Any]] = [
    {},
    {"choices": "not a list"},
    {"choices": [{"message": "not an object"}]},
    {"choices": [{}]},
]


@pytest.mark.parametrize("answer", NOT_A_COMPLETION)
def test_a_body_that_is_not_a_completion_fails_readably(answer: dict[str, Any]) -> None:
    """An endpoint that is not what it claimed should produce a failed round,
    not a stack trace inside a Worker."""
    response = normalize(answer)
    assert response.stop_reason is StopReason.FAILED
    assert response.failure is not None


def test_every_failure_is_safe_to_replay() -> None:
    """Nothing was written anywhere, so `retry` stays on the table.

    `external_effect_unknown` belongs to a request whose response never
    arrived, which is a transport question and not a parsing one.
    """
    for answer in [
        body(choices=choice(finish_reason="length")),
        body(choices=choice(finish_reason="content_filter")),
        body(choices=[]),
        {},
    ]:
        response = normalize(answer)
        assert response.stop_reason is StopReason.FAILED
        assert response.replay_safe is True
        assert response.external_effect_unknown is False


def test_a_round_is_always_one_model_call() -> None:
    assert normalize(body()).model_calls == 1
    assert normalize({}).model_calls == 1
