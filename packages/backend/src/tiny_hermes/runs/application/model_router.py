"""Chooses which provider answers a round, from the policy the Agent published.

The Worker holds one ``ModelProvider``. Which one a Run actually gets is decided
by the Agent Version it fixed at creation, so the choice has to happen per round
rather than per process — and it has to happen behind the same port, or the
Worker would start knowing about endpoints.

An endpoint that has vanished or been disabled since the Agent was published
produces a *failed round*, not an exception. A Run that outlives its endpoint is
an ordinary operational event, and the Run's own failure is where an operator
should read about it.
"""

import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.agents.domain.models import DeterministicModelPolicy
from tiny_hermes.model_catalog.infrastructure.credentials import (
    CredentialMissing,
    CredentialResolver,
)
from tiny_hermes.model_catalog.infrastructure.sql_store import SqlModelEndpointStore
from tiny_hermes.outbound.client import SafeOutboundClient
from tiny_hermes.runs.infrastructure.openai_model import (
    OpenAICompatibleProvider,
    RetryPolicy,
)
from tiny_hermes.runs.ports.model import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
    StopReason,
    UsageQuality,
)
from tiny_hermes.secrets.infrastructure.sql_store import SqlSecretStore
from tiny_hermes.shared.metrics import MODEL_CALL_SECONDS, MODEL_TOKENS
from tiny_hermes.shared.tracing import tracer

ClientFactory = Callable[[], AbstractAsyncContextManager[SafeOutboundClient]]


def _unavailable(reason: str) -> ModelResponse:
    return ModelResponse(
        stop_reason=StopReason.FAILED,
        text="",
        usage_quality=UsageQuality.UNAVAILABLE,
        replay_safe=True,
        external_effect_unknown=False,
        failure=reason,
    )


class ModelRouter:
    """One ``ModelProvider`` that stands for all of them."""

    def __init__(
        self,
        deterministic: ModelProvider,
        session_factory: async_sessionmaker[AsyncSession],
        client_factory: ClientFactory,
        retry: RetryPolicy | None = None,
        kek: bytes | None = None,
    ) -> None:
        self._deterministic = deterministic
        self._sessions = session_factory
        self._client_factory = client_factory
        self._retry = retry or RetryPolicy()
        self._kek = kek

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Timed here because every round passes here (observability design
        §3.2). Labelled by provider kind, not endpoint: endpoints are a
        workspace's to create, and a label per endpoint has no bound."""
        provider = (
            "deterministic"
            if isinstance(request.policy, DeterministicModelPolicy)
            else "openai_compatible"
        )
        started = time.monotonic()
        with tracer.start_as_current_span(
            "model.call", attributes={"model.provider": provider}
        ) as span:
            try:
                response = await self._route(request)
            except BaseException:
                MODEL_CALL_SECONDS.labels(provider=provider, outcome="error").observe(
                    time.monotonic() - started
                )
                raise
            span.set_attribute("model.stop_reason", response.stop_reason.value)
        MODEL_CALL_SECONDS.labels(provider=provider, outcome=response.stop_reason.value).observe(
            time.monotonic() - started
        )
        if response.input_tokens is not None:
            MODEL_TOKENS.labels(provider=provider, direction="in").inc(response.input_tokens)
        if response.output_tokens is not None:
            MODEL_TOKENS.labels(provider=provider, direction="out").inc(response.output_tokens)
        return response

    async def _route(self, request: ModelRequest) -> ModelResponse:
        policy = request.policy
        if isinstance(policy, DeterministicModelPolicy):
            return await self._deterministic.complete(request)

        async with self._sessions() as session:
            endpoint = await SqlModelEndpointStore(session).read(policy.endpoint_id)
            if endpoint is None or not endpoint.is_selectable:
                return _unavailable("model_endpoint_unavailable")
            try:
                token = await CredentialResolver(SqlSecretStore(session), self._kek).resolve(
                    endpoint.spec.credential_ref
                )
            except CredentialMissing:
                return _unavailable("credential_missing")

        async with self._client_factory() as client:
            provider = OpenAICompatibleProvider(
                endpoint.spec, client, self._retry, token=token
            )
            return await provider.complete(request)
