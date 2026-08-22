from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from tiny_hermes.identity.domain.models import OidcProviderStatus


@dataclass(frozen=True)
class OidcProviderRecord:
    id: UUID
    issuer: str
    client_id: str
    client_secret_ref: str
    discovery_url: str
    scopes: tuple[str, ...]
    status: OidcProviderStatus
    created_by: UUID
    created_at: datetime


@dataclass(frozen=True)
class OidcLoginStateRecord:
    """What a consumed `oidc_login_states` row hands the callback. Not the
    row itself — `state` and `id`/`created_at` are spent the moment this is
    returned, and the caller has no further use for them."""

    provider_id: UUID
    nonce: str
    code_verifier: str
    redirect_uri: str


class OidcProviderStore(Protocol):
    async def create_provider(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret_ref: str,
        discovery_url: str,
        scopes: Sequence[str],
        created_by: UUID,
    ) -> OidcProviderRecord: ...

    async def get_provider(self, provider_id: UUID) -> OidcProviderRecord | None: ...

    async def list_providers(self) -> Sequence[OidcProviderRecord]: ...

    async def disable_provider(self, provider_id: UUID) -> OidcProviderRecord | None: ...

    async def create_login_state(
        self,
        *,
        provider_id: UUID,
        state: str,
        nonce: str,
        code_verifier: str,
        redirect_uri: str,
        expires_at: datetime,
    ) -> None: ...

    async def consume_login_state(
        self, state: str, provider_id: UUID, now: datetime
    ) -> OidcLoginStateRecord | None:
        """Single-use: a `state` already consumed, expired, or naming a
        different provider than the one on this callback's URL comes back
        `None` — the same collapsed refusal `handle_callback` gives every
        other failure in the exchange, so a replay cannot be told apart from
        a `state` that simply expired."""
        ...

    async def append_audit(
        self,
        *,
        actor_id: UUID | None,
        action: str,
        result: str,
        request_id: str,
        context: dict[str, str],
    ) -> None: ...
