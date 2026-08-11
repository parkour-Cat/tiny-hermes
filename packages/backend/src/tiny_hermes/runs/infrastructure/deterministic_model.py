import asyncio

from tiny_hermes.agents.domain.models import DeterministicModelPolicy
from tiny_hermes.runs.domain.models import ToolCallBlock, ToolResultBlock
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
        if scenario == "shell_once":
            results = tuple(
                block
                for message in request.messages
                for block in message.blocks
                if isinstance(block, ToolResultBlock) and block.call_id == "drill-shell-1"
            )
            if not results:
                return ModelResponse(
                    stop_reason=StopReason.TOOL_CALL,
                    text="Running the deterministic sandbox drill command.",
                    tool_calls=(
                        ToolCallBlock(
                            call_id="drill-shell-1",
                            name="shell.exec",
                            arguments={
                                "command": (
                                    "printf 'drill-started\\n'; sleep 20; "
                                    "printf 'drill-finished\\n'"
                                ),
                                "timeout_seconds": 60,
                            },
                        ),
                    ),
                    input_tokens=TOKENS_PER_ROUND // 2,
                    output_tokens=TOKENS_PER_ROUND // 2,
                )
            result = results[-1]
            if result.failed or "drill-finished" not in result.output:
                return ModelResponse(
                    stop_reason=StopReason.FAILED,
                    text="",
                    input_tokens=TOKENS_PER_ROUND // 2,
                    output_tokens=TOKENS_PER_ROUND // 2,
                    failure="deterministic_shell_output_missing",
                )
            return ModelResponse(
                stop_reason=StopReason.COMPLETED,
                text="The model observed real command output: drill-finished.",
                input_tokens=TOKENS_PER_ROUND // 2,
                output_tokens=TOKENS_PER_ROUND // 2,
            )
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
