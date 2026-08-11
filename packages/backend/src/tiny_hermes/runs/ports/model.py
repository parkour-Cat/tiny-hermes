"""What a Worker asks a model, and what a model may answer.

The request carries a conversation rather than a string. Phase 2B carried
``input_text`` and a round counter, which was enough for a stand-in that decides
in advance what round two will say and cannot be enough for anything else.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from tiny_hermes.agents.domain.models import ModelPolicy
from tiny_hermes.runs.domain.models import CanonicalMessage, ToolCallBlock


class StopReason(StrEnum):
    """Why a model round ended, normalized across providers."""

    COMPLETED = "completed"
    CONTINUE = "continue"
    #: The model asked for a tool. The round produced a request rather than an
    #: answer, so the loop executes it and comes back rather than finishing.
    TOOL_CALL = "tool_call"
    FAILED = "failed"


class UsageQuality(StrEnum):
    """How far this round's Token counts can be trusted.

    Mirrors the endpoint-level declaration in the model catalog, because what a
    single response actually carried is not always what the endpoint promised —
    and the count that gets billed has to come from the response.

    There is no ``estimated``. Technical design §9.4 admits an estimate only
    from a tokenizer verified against the model, and none exists here.
    """

    PROVIDER = "provider"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ModelRequest:
    policy: ModelPolicy
    personality: str
    #: The conversation so far, oldest first, ending with what the caller asked.
    messages: tuple[CanonicalMessage, ...]
    round_index: int


@dataclass(frozen=True)
class ModelResponse:
    """One completed model round.

    ``input_tokens`` and ``output_tokens`` are nullable rather than zero. Zero is
    a number the platform would then have to tell apart from "the endpoint
    reported nothing", and it has only been getting away with conflating them
    because the stand-in always reports.

    ``replay_safe`` and ``external_effect_unknown`` are reported by the provider
    rather than assumed by the caller, so a provider that cannot confirm an
    external side effect can say so instead of being presumed safe.
    """

    stop_reason: StopReason
    text: str
    model_calls: int = 1
    input_tokens: int | None = None
    output_tokens: int | None = None
    usage_quality: UsageQuality = UsageQuality.PROVIDER
    #: Present only when ``stop_reason`` is ``tool_call``. A tuple rather than
    #: one call, because a model may ask for two things at once and answering
    #: only the first leaves a question the model believes it asked.
    tool_calls: tuple[ToolCallBlock, ...] = ()
    replay_safe: bool = True
    external_effect_unknown: bool = False
    #: A normalized reason when ``stop_reason`` is ``failed``.
    failure: str | None = None

    @property
    def billable_tokens(self) -> int:
        """What may be added to the shared budget, which is nothing when unknown."""
        if self.usage_quality is UsageQuality.UNAVAILABLE:
            return 0
        return (self.input_tokens or 0) + (self.output_tokens or 0)


class ModelProvider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...
