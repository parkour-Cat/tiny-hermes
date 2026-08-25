"""Registering, describing, and probing approved model endpoints.

One deliberate departure from the phase-3A design document, which said the
address would be validated when an endpoint is registered: it is not. Address
safety is enforced at call time, on every request and every redirect hop, by
`SafeOutboundClient`, and that check cannot be bypassed. A second check at
registration would be a convenience that goes stale the moment DNS changes, and
two rules for one question are two rules that drift apart. What an administrator
gets instead is the `check` route, which makes a real call and reports what
happened — early feedback that is the same code path the Worker will take.
"""

import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from tiny_hermes.model_catalog.domain.models import (
    EndpointStatus,
    ModelEndpoint,
    ModelEndpointSpec,
)
from tiny_hermes.model_catalog.infrastructure import credentials
from tiny_hermes.model_catalog.infrastructure.credentials import CredentialResolver
from tiny_hermes.model_catalog.ports.store import EndpointNameTaken, ModelEndpointStore
from tiny_hermes.outbound.client import SafeOutboundClient
from tiny_hermes.outbound.errors import OutboundError, OutboundRefused
from tiny_hermes.secrets.ports.store import SecretStore
from tiny_hermes.shared.errors import AppError
from tiny_hermes.tenancy.domain.models import Actor


@dataclass(frozen=True)
class CheckResult:
    """What a connectivity probe is allowed to say.

    Never the endpoint's response body. A `base_url` mistyped into an internal
    service would otherwise turn this into a way to read it, which is the same
    attack the address policy exists to stop — one layer further in.
    """

    reachable: bool
    elapsed_ms: int
    refusal: str | None = None
    detail: str | None = None


def _forbidden() -> AppError:
    return AppError(
        code="forbidden",
        title="Forbidden",
        status=403,
        detail="Only a platform administrator can manage model endpoints.",
    )


class EndpointScopeWriter(Protocol):
    """The two writes an endpoint makes to the outbound scope, and no reads."""

    async def approve_endpoint_host(
        self, *, endpoint_id: UUID, host: str, created_by: UUID
    ) -> None: ...

    async def withdraw_endpoint_host(self, endpoint_id: UUID) -> int: ...


class ModelEndpointService:
    def __init__(
        self,
        store: ModelEndpointStore,
        secrets: SecretStore | None = None,
        kek: bytes | None = None,
        scopes: EndpointScopeWriter | None = None,
    ) -> None:
        self._store = store
        self._secrets = secrets
        self._kek = kek
        # Registering an endpoint approves the host it names. Optional so the
        # domain tests, which are about who may register what, keep reading the
        # way they did — and absent, an endpoint simply approves nothing, which
        # a platform administrator can still do by hand.
        self._scopes = scopes

    async def register(self, actor: Actor, spec: ModelEndpointSpec) -> ModelEndpoint:
        if not actor.is_platform_admin:
            raise _forbidden()
        await self._require_credential(spec)
        try:
            registered = await self._store.register(spec, created_by=actor.id)
        except EndpointNameTaken as clash:
            raise AppError(
                code="endpoint_name_taken",
                title="Endpoint name taken",
                status=409,
                detail="Another model endpoint already uses this name.",
            ) from clash
        await self._approve_host(registered, actor)
        return registered

    async def update(
        self, actor: Actor, endpoint_id: UUID, spec: ModelEndpointSpec
    ) -> ModelEndpoint:
        if not actor.is_platform_admin:
            raise _forbidden()
        await self._require_credential(spec)
        try:
            updated = await self._store.update(endpoint_id, spec)
        except EndpointNameTaken as clash:
            raise AppError(
                code="endpoint_name_taken",
                title="Endpoint name taken",
                status=409,
                detail="Another model endpoint already uses this name.",
            ) from clash
        if updated is None:
            raise self._unknown()
        # The host may have moved. The old entry goes with it, so a `base_url`
        # that was corrected does not leave its predecessor approved.
        await self._withdraw_host(endpoint_id)
        await self._approve_host(updated, actor)
        return updated

    async def amend(
        self,
        actor: Actor,
        endpoint_id: UUID,
        *,
        status: EndpointStatus | None = None,
        accepts_images: bool | None = None,
    ) -> ModelEndpoint:
        """Change what may be changed after registration.

        `None` means unchanged for each, so a caller naming one field cannot
        reset the other underneath itself.

        `accepts_images` is a statement about the endpoint rather than a
        choice of endpoint, which is why it is amendable at all while
        `model` and `base_url` are not — changing either of those swaps the
        endpoint for a different one beneath every AgentVersion that named
        it.
        """
        if not actor.is_platform_admin:
            raise _forbidden()
        if accepts_images is not None:
            amended = await self._store.set_accepts_images(
                endpoint_id, accepts_images
            )
            if amended is None:
                raise self._unknown()
        if status is None:
            found = await self._store.read(endpoint_id)
            if found is None:
                raise self._unknown()
            return found
        return await self.set_status(actor, endpoint_id, status)

    async def set_status(
        self, actor: Actor, endpoint_id: UUID, status: EndpointStatus
    ) -> ModelEndpoint:
        if not actor.is_platform_admin:
            raise _forbidden()
        updated = await self._store.set_status(endpoint_id, status)
        if updated is None:
            raise self._unknown()
        # An endpoint nobody may select is an endpoint nothing should be able
        # to reach: the approval exists because the endpoint does.
        if updated.is_selectable:
            await self._approve_host(updated, actor)
        else:
            await self._withdraw_host(endpoint_id)
        return updated

    async def _approve_host(self, endpoint: ModelEndpoint, actor: Actor) -> None:
        """Registering an endpoint is the approval; there is no second one.

        Requiring an administrator to write the host into the outbound scope as
        well would add a step that gets forgotten rather than a judgement that
        gets made — and the symptom of forgetting it is a Run failing at
        runtime, a long way from the cause.
        """
        if self._scopes is None:
            return
        host = urlsplit(endpoint.spec.base_url).hostname
        if not host:  # pragma: no cover - a spec without a host does not validate
            return
        await self._scopes.approve_endpoint_host(
            endpoint_id=endpoint.id, host=host, created_by=actor.id
        )

    async def _withdraw_host(self, endpoint_id: UUID) -> None:
        if self._scopes is None:
            return
        await self._scopes.withdraw_endpoint_host(endpoint_id)

    async def read(self, actor: Actor, endpoint_id: UUID) -> ModelEndpoint:
        if not actor.is_platform_admin:
            raise _forbidden()
        found = await self._store.read(endpoint_id)
        if found is None:
            raise self._unknown()
        return found

    async def list_selectable(self) -> list[ModelEndpoint]:
        """Readable by any signed-in user.

        The list carries no credential and no address — only which models this
        platform offers, which every user who can publish an Agent has to be
        able to choose from.
        """
        return await self._store.list_active()

    async def check(
        self, actor: Actor, endpoint_id: UUID, client: SafeOutboundClient
    ) -> CheckResult:
        endpoint = await self.read(actor, endpoint_id)
        started = time.monotonic()
        try:
            token = await CredentialResolver(self._secrets, self._kek).resolve(
                endpoint.spec.credential_ref
            )
        except credentials.CredentialMissing:
            return CheckResult(
                reachable=False, elapsed_ms=self._since(started), refusal="credential_missing"
            )
        try:
            await client.post(
                f"{endpoint.spec.base_url}/chat/completions",
                json={
                    "model": endpoint.spec.model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
        except OutboundRefused as refusal:
            return CheckResult(
                reachable=False,
                elapsed_ms=self._since(started),
                refusal=refusal.reason.value,
            )
        except OutboundError as failure:
            # The class name, not the message: a connection error's text carries
            # the address it failed to reach.
            return CheckResult(
                reachable=False,
                elapsed_ms=self._since(started),
                refusal="unreachable",
                detail=type(failure).__name__,
            )
        # A status is not read here on purpose. The endpoint answered, which is
        # what this route asks; whether it liked the request is a question for
        # a Run, and reporting its status would start to describe its body.
        return CheckResult(reachable=True, elapsed_ms=self._since(started))

    async def credential_available(self, endpoint: ModelEndpoint) -> bool:
        return await CredentialResolver(self._secrets, None).is_available(
            endpoint.spec.credential_ref
        )

    @staticmethod
    def _since(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    @staticmethod
    def _unknown() -> AppError:
        return AppError(
            code="model_endpoint_not_found",
            title="Model endpoint not found",
            status=404,
            detail="No such model endpoint exists.",
        )

    async def _require_credential(self, spec: ModelEndpointSpec) -> None:
        if await CredentialResolver(self._secrets, None).is_available(spec.credential_ref):
            return
        raise AppError(
            code="credential_missing",
            title="Credential missing",
            status=422,
            detail=(
                "The deployment does not define the environment variable this "
                "endpoint names, and no active Secret has that id. Set the "
                "variable or store the Secret before registering the endpoint."
            ),
        )
