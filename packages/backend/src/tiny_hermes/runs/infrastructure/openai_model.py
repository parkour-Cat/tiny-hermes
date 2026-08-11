"""An OpenAI-compatible endpoint, as one of this platform's model providers.

Not streaming. `stream` is not sent and no merge path exists, because nothing in
phase 3A consumes partial text: the console renders Run events, not tokens. The
merge belongs with Chat Completions in phase four, where it has a consumer that
can hold it to account. This is a deliberate omission rather than an oversight,
which is why it is stated here.

Every parsing failure produces a *failed round*, never an exception out of the
Worker, and every one of them is replay-safe: nothing was written anywhere, so
the Run may be retried. `external_effect_unknown` belongs to a request whose
response never arrived — a transport question, decided by the outbound client.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from tiny_hermes.agents.domain.models import EndpointModelPolicy
from tiny_hermes.model_catalog.domain.models import ModelEndpointSpec
from tiny_hermes.model_catalog.infrastructure import credentials
from tiny_hermes.outbound.client import SafeOutboundClient
from tiny_hermes.outbound.errors import OutboundError, OutboundRefused
from tiny_hermes.runs.domain.models import CanonicalMessage
from tiny_hermes.runs.ports.model import (
    ModelRequest,
    ModelResponse,
    StopReason,
    UsageQuality,
)

logger = logging.getLogger(__name__)

#: Prepended to every conversation, ahead of the Agent's own personality. Fixed
#: text in this slice: a configurable safety preamble is a policy surface, and
#: there is nowhere to administer one yet.
SAFETY_PREAMBLE = (
    "You are running inside tiny-hermes, a controlled execution platform. "
    "You have no tools, no file access, and no network access. "
    "Answer with text only. If a request needs a capability you do not have, "
    "say so plainly instead of pretending to act."
)

#: Statuses worth trying again. A rejection on the merits will be rejected
#: again, and three attempts would turn one clear failure into a
#: three-times-slower identical failure.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_ms: int = 250

    def backoff_seconds(self, attempt: int) -> float:
        """Exponential, with jitter, so a fleet of Workers does not resynchronize."""
        ceiling = self.base_ms * (2 ** (attempt - 1))
        return random.uniform(0, ceiling) / 1000  # noqa: S311 - jitter, not a secret


def _failed(reason: str) -> ModelResponse:
    return ModelResponse(
        stop_reason=StopReason.FAILED,
        text="",
        usage_quality=UsageQuality.UNAVAILABLE,
        replay_safe=True,
        external_effect_unknown=False,
        failure=reason,
    )


def _usage(body: dict[str, Any]) -> tuple[int | None, int | None, UsageQuality]:
    """Both counts, or neither.

    One of the two is not a usage report; it is half of one, and half a report
    charged against a Token limit is worse than an honest "unknown".
    """
    reported: Any = body.get("usage")
    if not isinstance(reported, dict):
        return None, None, UsageQuality.UNAVAILABLE
    fields = cast(dict[str, Any], reported)
    prompt: Any = fields.get("prompt_tokens")
    completion: Any = fields.get("completion_tokens")
    if isinstance(prompt, bool) or isinstance(completion, bool):
        return None, None, UsageQuality.UNAVAILABLE
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return None, None, UsageQuality.UNAVAILABLE
    return prompt, completion, UsageQuality.PROVIDER


def normalize(body: dict[str, Any]) -> ModelResponse:
    """One endpoint answer, as one platform round."""
    choices: Any = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return _failed("empty_response")
    first: Any = cast(list[Any], choices)[0]
    if not isinstance(first, dict):
        return _failed("malformed_choice")
    entry = cast(dict[str, Any], first)

    finish: Any = entry.get("finish_reason")
    if finish == "tool_calls":
        # No tool is bound and the model was told about none, so a request for
        # one has left the contract. Inventing a result or silently returning
        # whatever text came alongside would both be worse than stopping.
        return _failed("tool_use_not_supported")
    if finish == "length":
        return _failed("max_output_reached")
    if finish not in ("stop", None):
        return _failed("unsupported_stop_reason")

    message: Any = entry.get("message")
    if not isinstance(message, dict):
        return _failed("malformed_choice")
    content: Any = cast(dict[str, Any], message).get("content")
    text = content if isinstance(content, str) else ""
    if not text.strip():
        return _failed("empty_response")

    prompt_tokens, completion_tokens, quality = _usage(body)
    return ModelResponse(
        stop_reason=StopReason.COMPLETED,
        text=text,
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        usage_quality=quality,
    )


def build_payload(spec: ModelEndpointSpec, request: ModelRequest) -> dict[str, Any]:
    """The request body, with the platform's rules ahead of the Agent's persona.

    An Agent placed underneath the preamble cannot talk its way past it.

    The Agent's own output ceiling narrows the endpoint's, never widens it —
    publishing already refused a policy that asked for more, and taking the
    minimum here means a later change to that check cannot turn into a request
    the endpoint was never approved for.
    """
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SAFETY_PREAMBLE},
        {"role": "system", "content": request.personality},
    ]
    messages.extend(_as_message(entry) for entry in request.messages)
    payload: dict[str, Any] = {
        "model": spec.model,
        "messages": messages,
        "max_tokens": spec.max_output_tokens,
    }
    policy = request.policy
    if isinstance(policy, EndpointModelPolicy):
        if policy.max_output_tokens is not None:
            payload["max_tokens"] = min(policy.max_output_tokens, spec.max_output_tokens)
        if policy.temperature is not None:
            payload["temperature"] = policy.temperature
    return payload


def _as_message(message: CanonicalMessage) -> dict[str, str]:
    return {"role": message.role, "content": message.text}


class OpenAICompatibleProvider:
    """Calls one registered endpoint, through the guarded outbound client."""

    def __init__(
        self,
        spec: ModelEndpointSpec,
        client: SafeOutboundClient,
        policy: RetryPolicy | None = None,
        sleep: Sleeper | None = None,
    ) -> None:
        self._spec = spec
        self._client = client
        self._policy = policy or RetryPolicy()
        self._sleep = sleep or asyncio.sleep

    async def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            token = credentials.resolve(self._spec.credential_ref)
        except credentials.CredentialMissing:
            return _failed("credential_missing")

        url = f"{self._spec.base_url}/chat/completions"
        payload = build_payload(self._spec, request)
        last: ModelResponse = _failed("unreachable")
        for attempt in range(1, self._policy.max_attempts + 1):
            try:
                answer = await self._client.post(
                    url, json=payload, headers={"Authorization": f"Bearer {token}"}
                )
            except OutboundRefused as refusal:
                # A refused address will be refused identically next time.
                return _failed(f"outbound_refused:{refusal.reason.value}")
            except OutboundError as failure:
                last = ModelResponse(
                    stop_reason=StopReason.FAILED,
                    text="",
                    usage_quality=UsageQuality.UNAVAILABLE,
                    replay_safe=True,
                    external_effect_unknown=failure.external_effect_unknown,
                    failure="endpoint_unreachable",
                )
            else:
                if answer.status_code == 200:
                    return normalize(_json(answer.text))
                if answer.status_code not in RETRYABLE_STATUSES:
                    return _failed(f"endpoint_status:{answer.status_code}")
                last = _failed(f"endpoint_status:{answer.status_code}")
            if attempt < self._policy.max_attempts:
                await self._sleep(self._policy.backoff_seconds(attempt))
        return last


def _json(text: str) -> dict[str, Any]:
    import json

    try:
        parsed: Any = json.loads(text)
    except ValueError:
        return {}
    return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else {}
