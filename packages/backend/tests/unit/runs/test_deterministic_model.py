import pytest
from tiny_hermes.agents.domain.models import AgentSpec
from tiny_hermes.runs.domain.models import CanonicalMessage
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.ports.model import ModelRequest, StopReason

from ..agents.test_agent_models import valid_spec


def _request(scenario: str, round_index: int) -> ModelRequest:
    values = {
        **valid_spec(),
        "model_policy": {"provider": "deterministic", "scenario": scenario},
    }
    spec = AgentSpec.model_validate(values)
    return ModelRequest(
        policy=spec.model_policy,
        personality=spec.personality,
        messages=(CanonicalMessage(role="user", text="do the thing"),),
        round_index=round_index,
    )


async def test_complete_finishes_in_one_round() -> None:
    response = await DeterministicModelProvider(delay_ms=0).complete(
        _request("complete", 1)
    )

    assert response.stop_reason is StopReason.COMPLETED
    assert response.text
    assert response.replay_safe is True
    assert response.model_calls == 1


async def test_fail_replay_safe_fails_without_an_unknown_effect() -> None:
    response = await DeterministicModelProvider(delay_ms=0).complete(
        _request("fail_replay_safe", 1)
    )

    assert response.stop_reason is StopReason.FAILED
    assert response.replay_safe is True
    assert response.external_effect_unknown is False


async def test_continue_once_needs_a_second_round() -> None:
    provider = DeterministicModelProvider(delay_ms=0)

    first = await provider.complete(_request("continue_once", 1))
    second = await provider.complete(_request("continue_once", 2))

    assert first.stop_reason is StopReason.CONTINUE
    assert second.stop_reason is StopReason.COMPLETED


async def test_continue_once_never_continues_forever() -> None:
    provider = DeterministicModelProvider(delay_ms=0)

    for round_index in range(2, 6):
        response = await provider.complete(_request("continue_once", round_index))
        assert response.stop_reason is StopReason.COMPLETED


async def test_the_provider_is_pure_for_the_same_round() -> None:
    provider = DeterministicModelProvider(delay_ms=0)

    first = await provider.complete(_request("complete", 1))
    second = await provider.complete(_request("complete", 1))

    assert first == second


async def test_every_response_reports_one_model_call_and_some_usage() -> None:
    provider = DeterministicModelProvider(delay_ms=0)

    for scenario in ("complete", "fail_replay_safe", "continue_once"):
        response = await provider.complete(_request(scenario, 1))
        assert response.model_calls == 1
        assert response.billable_tokens > 0


@pytest.mark.parametrize("delay", [-1, 5001])
def test_the_delay_is_bounded(delay: int) -> None:
    with pytest.raises(ValueError, match="delay"):
        DeterministicModelProvider(delay_ms=delay)
