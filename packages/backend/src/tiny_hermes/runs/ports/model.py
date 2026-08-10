from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from tiny_hermes.agents.domain.models import DeterministicModelPolicy


class StopReason(StrEnum):
    """Why a model round ended, normalized across providers."""

    COMPLETED = "completed"
    CONTINUE = "continue"
    FAILED = "failed"


@dataclass(frozen=True)
class ModelRequest:
    policy: DeterministicModelPolicy
    personality: str
    input_text: str
    round_index: int


@dataclass(frozen=True)
class ModelResponse:
    """One completed model round.

    ``replay_safe`` and ``external_effect_unknown`` are reported by the provider
    rather than assumed by the caller, so a future provider that cannot confirm
    an external side effect can say so instead of being presumed safe.
    """

    stop_reason: StopReason
    text: str
    model_calls: int = 1
    tokens: int = 0
    replay_safe: bool = True
    external_effect_unknown: bool = False


class ModelProvider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...
