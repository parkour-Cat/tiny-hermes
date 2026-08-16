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
