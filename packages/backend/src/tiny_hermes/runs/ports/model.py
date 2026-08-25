"""What a Worker asks a model, and what a model may answer.

The request carries a conversation rather than a string. Phase 2B carried
``input_text`` and a round counter, which was enough for a stand-in that decides
in advance what round two will say and cannot be enough for anything else.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from tiny_hermes.agents.domain.models import ModelPolicy
from tiny_hermes.runs.domain.models import (
    CacheStateHint,
    CanonicalMessage,
    ToolCallBlock,
)


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
    #: The tools this AgentVersion bound, as provider schemas. §10.2's first
    #: step: an Agent that bound none advertises none.
    tools: tuple[dict[str, Any], ...] = ()
    #: Every `ImageBlock.reference` in `messages`, already resolved to a data
    #: URL. Resolved by the Worker rather than the provider: fetching a
    #: Feishu image needs that binding's app secret, and a model adapter has
    #: no business holding a channel's credentials. The provider only sends
    #: what it was handed, and refuses when a block names something absent —
    #: which is why this is a map rather than the bytes inline.
    images: dict[str, str] = field(default_factory=dict[str, str])
    #: One line per bound skill: what it is called and what it is for, the
    #: whole of what the model is told before it asks for any of the text.
    #:
    #: Its own field rather than something the caller glued onto
    #: ``personality``. Glued on, the two would be one string on the budget
    #: table, and neither the planner nor a person reading a Run could say
    #: which of them cost what — or trim one without touching the other.
    skill_summaries: tuple[str, ...] = ()
    #: What this Run's subject and this Agent's workspace decided is worth
    #: remembering, oldest first within each. Its own field for the reason
    #: `skill_summaries` is one: glued onto the persona they would be one
    #: string on the budget table, and neither the planner nor a person
    #: reading a Run could say which of them cost what.
    #:
    #: Never a raw user message. §14.2 forbids it for shared memory and this
    #: platform extends it to private: every line here is something somebody
    #: proposed and something a policy admitted.
    memories: tuple[str, ...] = ()
    #: Set when this slice began on a fresh writable layer. The provider places
    #: it with the platform's own rules rather than in the conversation, so a
    #: later turn cannot talk over it.
    cache_hint: CacheStateHint | None = None


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
    #: A thinking model's own reasoning, when the endpoint sent some.
    #: `None` rather than empty, because the two decide different things:
    #: DeepSeek requires this back on the next request, and a field invented
    #: for an endpoint that never sent one is a request it may refuse.
    reasoning: str | None = None

    @property
    def billable_tokens(self) -> int:
        """What may be added to the shared budget, which is nothing when unknown."""
        if self.usage_quality is UsageQuality.UNAVAILABLE:
            return 0
        return (self.input_tokens or 0) + (self.output_tokens or 0)


class ModelProvider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...
