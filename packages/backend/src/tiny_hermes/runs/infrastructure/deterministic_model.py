import asyncio

from tiny_hermes.agents.domain.models import DeterministicModelPolicy
from tiny_hermes.runs.ports.model import (
    ModelRequest,
    ModelResponse,
    StopReason,
    UsageQuality,
)

MAX_DELAY_MS = 5_000
TOKENS_PER_ROUND = 32


class DeterministicModelProvider:
    """The only phase-2B model provider.

    It is not a test double. A published Agent Version selects one of its
    scenarios through a validated model policy, and the provider answers the
    same way every time so a Run's behavior is reproducible. It performs no
    network call, so no outbound policy applies to it yet.
    """

    def __init__(self, delay_ms: int = 50) -> None:
        if not 0 <= delay_ms <= MAX_DELAY_MS:
            raise ValueError(f"delay must be between 0 and {MAX_DELAY_MS} milliseconds")
        self._delay_seconds = delay_ms / 1000

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        policy = request.policy
        if not isinstance(policy, DeterministicModelPolicy):
            # Reaching here is a routing mistake, not a user's. It fails the
            # round rather than raising, because a Worker that crashes on a
            # misrouted Run loses the whole slice instead of one Run.
            return ModelResponse(
                stop_reason=StopReason.FAILED,
                text="",
                usage_quality=UsageQuality.UNAVAILABLE,
                failure="policy_mismatch",
            )
        scenario = policy.scenario
        if scenario == "fail_replay_safe":
            return ModelResponse(
                stop_reason=StopReason.FAILED,
                text="The deterministic scenario stopped at a replay-safe checkpoint.",
                input_tokens=TOKENS_PER_ROUND // 2,
                output_tokens=TOKENS_PER_ROUND // 2,
                replay_safe=True,
                external_effect_unknown=False,
            )
        if scenario == "continue_once" and request.round_index <= 1:
            return ModelResponse(
                stop_reason=StopReason.CONTINUE,
                text="The deterministic scenario needs one more round.",
                input_tokens=TOKENS_PER_ROUND // 2,
                output_tokens=TOKENS_PER_ROUND // 2,
            )
        return ModelResponse(
            stop_reason=StopReason.COMPLETED,
            text="The deterministic scenario finished.",
            input_tokens=TOKENS_PER_ROUND // 2,
            output_tokens=TOKENS_PER_ROUND // 2,
        )
