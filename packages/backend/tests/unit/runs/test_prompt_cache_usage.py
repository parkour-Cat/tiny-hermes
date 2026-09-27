"""How many prompt tokens hit the provider's cache, read and charged.

Providers bill a cached prompt prefix at a lower rate, and report how much of
the prompt was cached. This platform read only `prompt_tokens` and
`completion_tokens`, so `cost_of` — which already takes `cached_input_tokens`
and a `cached_input_per_million` price — charged every cached token at the
full input rate, and nobody could see whether the cache was hit at all.

The usage bodies below are the providers' own shapes, not ones written from
memory (CLAUDE.md: a fixture for another party's data is built from that
party's definition):

- OpenAI: `CompletionUsage` / `PromptTokensDetails` in the official SDK,
  `openai/openai-python` `src/openai/types/completion_usage.py` at 43443d14c5
  (2026-09-26). `prompt_tokens_details` and its `cached_tokens` are optional.
  The SDK is not a dependency here, so the dict is spelled field for field
  from that file rather than constructed from the class.
- DeepSeek: the example `usage` object in its Create Chat Completion reference
  (api-docs.deepseek.com/api/create-chat-completion), copied verbatim, with
  the hit count changed from 0 where a test needs a hit.
"""

from decimal import Decimal
from typing import Any

from tiny_hermes.model_catalog.domain.pricing import TokenPrices
from tiny_hermes.runs.application.worker import (
    _checkpoint,  # pyright: ignore[reportPrivateUsage]
    _cost_from,  # pyright: ignore[reportPrivateUsage]
    _summary_billed_payload,  # pyright: ignore[reportPrivateUsage]
)
from tiny_hermes.runs.infrastructure.openai_model import normalize
from tiny_hermes.runs.ports.model import UsageQuality


def _body(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "the answer"},
            }
        ],
        "usage": usage,
    }


#: OpenAI `CompletionUsage`, with `PromptTokensDetails.cached_tokens` set.
OPENAI_USAGE: dict[str, Any] = {
    "completion_tokens": 20,
    "prompt_tokens": 2_048,
    "total_tokens": 2_068,
    "completion_tokens_details": {
        "accepted_prediction_tokens": 0,
        "audio_tokens": 0,
        "reasoning_tokens": 0,
        "rejected_prediction_tokens": 0,
    },
    "prompt_tokens_details": {"audio_tokens": 0, "cached_tokens": 1_920},
}

#: DeepSeek's documented example, verbatim.
DEEPSEEK_EXAMPLE: dict[str, Any] = {
    "completion_tokens": 9,
    "prompt_tokens": 17,
    "total_tokens": 26,
    "prompt_tokens_details": {"cached_tokens": 0},
    "prompt_cache_hit_tokens": 0,
    "prompt_cache_miss_tokens": 17,
}


def test_openai_cached_tokens_are_read() -> None:
    response = normalize(_body(OPENAI_USAGE))

    assert response.input_tokens == 2_048
    assert response.cached_input_tokens == 1_920


def test_deepseek_documented_usage_is_read() -> None:
    response = normalize(_body(DEEPSEEK_EXAMPLE))

    assert response.input_tokens == 17
    assert response.cached_input_tokens == 0


def test_deepseek_top_level_hit_count_is_read_when_the_details_are_absent() -> None:
    usage = {
        key: value for key, value in DEEPSEEK_EXAMPLE.items() if key != "prompt_tokens_details"
    }
    usage["prompt_cache_hit_tokens"] = 12
    usage["prompt_cache_miss_tokens"] = 5

    assert normalize(_body(usage)).cached_input_tokens == 12


def test_no_cache_report_is_unknown_not_zero() -> None:
    """An endpoint that says nothing about its cache has not said "no hits"."""
    response = normalize(_body({"prompt_tokens": 11, "completion_tokens": 7}))

    assert response.cached_input_tokens is None
    assert response.usage_quality is UsageQuality.PROVIDER


def test_a_cache_count_that_cannot_be_true_is_ignored_not_fatal() -> None:
    """More cached than sent, a negative count or a boolean: the round's own
    usage still stands; only the discount is withheld."""
    for bad in (5_000, -1, True, "12"):
        usage = {**OPENAI_USAGE, "prompt_tokens_details": {"cached_tokens": bad}}
        response = normalize(_body(usage))
        assert response.cached_input_tokens is None, bad
        assert response.input_tokens == 2_048
        assert response.usage_quality is UsageQuality.PROVIDER


PRICES = TokenPrices(
    currency="USD",
    input_per_million=Decimal("2"),
    output_per_million=Decimal("8"),
    cached_input_per_million=Decimal("0.5"),
)


def test_a_round_that_hit_the_cache_is_charged_the_cached_rate() -> None:
    cached = _cost_from(normalize(_body(OPENAI_USAGE)), PRICES)
    uncached_usage = {**OPENAI_USAGE, "prompt_tokens_details": {"cached_tokens": 0}}
    uncached = _cost_from(normalize(_body(uncached_usage)), PRICES)

    assert cached is not None and uncached is not None
    # 128 plain at 2/M + 1,920 cached at 0.5/M + 20 out at 8/M.
    assert cached.amount == Decimal("0.001376")
    assert cached.amount < uncached.amount


def test_the_round_record_says_how_much_was_cached() -> None:
    """Where an operator sees whether the cache is being hit at all."""
    assert _checkpoint(normalize(_body(OPENAI_USAGE)))["cached_input_tokens"] == 1_920
    silent = normalize(_body({"prompt_tokens": 11, "completion_tokens": 7}))
    assert "cached_input_tokens" not in _checkpoint(silent)


def test_the_summary_billing_event_says_how_much_was_cached() -> None:
    """It lists what the provider reported so an operator can reconcile the
    cost; the cost now depends on the cached count, so the count is listed."""
    response = normalize(_body(OPENAI_USAGE))
    cost = _cost_from(response, PRICES)
    assert cost is not None

    payload = _summary_billed_payload(None, "acme", response, cost)

    assert payload["cached_input_tokens"] == 1_920
