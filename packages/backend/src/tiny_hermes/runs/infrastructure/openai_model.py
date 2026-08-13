"""An OpenAI-compatible endpoint, as one of this platform's model providers.

Inbound Chat Completions streaming is a delivery concern of `POST /v1/chat/completions`.
This outbound adapter still buffers one round: partial tokens are not RunEvents,
and the Worker checkpoint needs the complete assistant turn. The HTTP adapter
streams that turn to the compatibility client after the round is recorded.
"""

Every parsing failure produces a *failed round*, never an exception out of the
Worker, and every one of them is replay-safe: nothing was written anywhere, so
the Run may be retried. `external_effect_unknown` belongs to a request whose
response never arrived — a transport question, decided by the outbound client.
"""

import asyncio
import json
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
from tiny_hermes.runs.domain.models import (
    CACHE_RESET_HINT,
    CacheStateHint,
    CanonicalMessage,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
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

    message_any: Any = entry.get("message")
    finish: Any = entry.get("finish_reason")
    if finish == "tool_calls":
        # Phase 3A refused this, because no tool was bound and a model asking
        # for one had left the contract. One is bound now, so it is a parse.
        return _tool_round(body, message_any)
    if finish == "length":
        return _failed("max_output_reached")
    if finish not in ("stop", None):
        return _failed("unsupported_stop_reason")

    message: Any = message_any
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


def _tool_round(body: dict[str, Any], message: Any) -> ModelResponse:
    """A round that asked for tools rather than answering."""
    if not isinstance(message, dict):
        return _failed("malformed_choice")
    fields = cast(dict[str, Any], message)
    raw: Any = fields.get("tool_calls")
    if not isinstance(raw, list) or not raw:
        return _failed("malformed_tool_call")

    calls: list[ToolCallBlock] = []
    for entry in cast(list[Any], raw):
        if not isinstance(entry, dict):
            return _failed("malformed_tool_call")
        call = cast(dict[str, Any], entry)
        function: Any = call.get("function")
        if not isinstance(function, dict):
            return _failed("malformed_tool_call")
        signature = cast(dict[str, Any], function)
        try:
            # Decoded once, here at the edge. Carrying the provider's JSON
            # string inward would make every reader decode it, and each one
            # could decide differently what a malformed one means.
            arguments: Any = json.loads(str(signature.get("arguments") or "{}"))
        except ValueError:
            return _failed("malformed_tool_arguments")
        if not isinstance(arguments, dict):
            return _failed("malformed_tool_arguments")
        try:
            calls.append(
                ToolCallBlock(
                    call_id=str(call.get("id") or ""),
                    name=str(signature.get("name") or ""),
                    arguments=cast(dict[str, Any], arguments),
                )
            )
        except ValueError:
            # A call with no id cannot be answered and one with no name cannot
            # be dispatched. Either is a failed round, not a guess.
            return _failed("malformed_tool_call")

    content: Any = fields.get("content")
    prompt_tokens, completion_tokens, quality = _usage(body)
    return ModelResponse(
        stop_reason=StopReason.TOOL_CALL,
        text=content if isinstance(content, str) else "",
        tool_calls=tuple(calls),
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        usage_quality=quality,
    )


def build_payload(
    spec: ModelEndpointSpec,
    request: ModelRequest,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The request body, with the platform's rules ahead of the Agent's persona.

    An Agent placed underneath the preamble cannot talk its way past it.

    The Agent's own output ceiling narrows the endpoint's, never widens it —
    publishing already refused a policy that asked for more, and taking the
    minimum here means a later change to that check cannot turn into a request
    the endpoint was never approved for.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SAFETY_PREAMBLE},
        {"role": "system", "content": request.personality},
    ]
    if request.cache_hint is CacheStateHint.RESET:
        # With the platform's rules rather than in the conversation, so a later
        # turn cannot talk over it. §11.3 calls it a protected runtime hint.
        messages.append({"role": "system", "content": CACHE_RESET_HINT})
    for entry in request.messages:
        messages.extend(_as_messages(entry))
    payload: dict[str, Any] = {
        "model": spec.model,
        "messages": messages,
        "max_tokens": spec.max_output_tokens,
    }
    advertised = tools if tools is not None else list(request.tools)
    if advertised:
        # §10.2's first step: a model told about no tool cannot correctly ask
        # for one, so an Agent that binds none advertises none.
        payload["tools"] = advertised
    policy = request.policy
    if isinstance(policy, EndpointModelPolicy):
        if policy.max_output_tokens is not None:
            payload["max_tokens"] = min(policy.max_output_tokens, spec.max_output_tokens)
        if policy.temperature is not None:
            payload["temperature"] = policy.temperature
    return payload


def _as_messages(message: CanonicalMessage) -> list[dict[str, Any]]:
    """One canonical turn, as the one-or-more messages the API expects.

    It walks `blocks` rather than `message.text`. The text accessor still
    exists and still compiles, so an adapter that used it would send a
    conversation with every call and result missing — and the model would then
    answer as though it had never acted, which is the worst possible way for
    this to be wrong.
    """
    results = [b for b in message.blocks if isinstance(b, ToolResultBlock)]
    if results:
        # One message per call id: the API ties each result to the call it
        # answers, and merging them would leave a call unanswered.
        return [
            {"role": "tool", "tool_call_id": block.call_id, "content": block.output}
            for block in results
        ]

    text = "".join(b.text for b in message.blocks if isinstance(b, TextBlock))
    calls = [b for b in message.blocks if isinstance(b, ToolCallBlock)]
    if not calls:
        return [{"role": message.role, "content": text}]
    return [
        {
            "role": "assistant",
            # Null rather than empty when the model acted without speaking,
            # which is what the API expects and what actually happened.
            "content": text or None,
            "tool_calls": [
                {
                    "id": block.call_id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.arguments),
                    },
                }
                for block in calls
            ],
        }
    ]


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
