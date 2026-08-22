from collections.abc import Sequence
from datetime import datetime
from uuid import UUID, uuid4

from tiny_hermes.identity.domain.models import OidcProviderStatus
from tiny_hermes.identity.ports.oidc_store import OidcLoginStateRecord, OidcProviderRecord


class MemoryOidcProviderStore:
    def __init__(self) -> None:
        self._providers: dict[UUID, OidcProviderRecord] = {}
        self._states: dict[str, tuple[UUID, OidcLoginStateRecord, datetime, datetime | None]] = {}
        self.audit_actions: list[str] = []

    async def create_provider(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret_ref: str,
        discovery_url: str,
        scopes: Sequence[str],
        created_by: UUID,
    ) -> OidcProviderRecord:
        record = OidcProviderRecord(
            id=uuid4(),
            issuer=issuer,
            client_id=client_id,
            client_secret_ref=client_secret_ref,
            discovery_url=discovery_url,
            scopes=tuple(scopes),
            status=OidcProviderStatus.ACTIVE,
            created_by=created_by,
            created_at=datetime.now(),
        )
        self._providers[record.id] = record
        return record

    async def get_provider(self, provider_id: UUID) -> OidcProviderRecord | None:
        return self._providers.get(provider_id)

    async def list_providers(self) -> Sequence[OidcProviderRecord]:
        return list(self._providers.values())

    async def disable_provider(self, provider_id: UUID) -> OidcProviderRecord | None:
        record = self._providers.get(provider_id)
        if record is None:
            return None
        disabled = OidcProviderRecord(
            id=record.id,
            issuer=record.issuer,
            client_id=record.client_id,
            client_secret_ref=record.client_secret_ref,
            discovery_url=record.discovery_url,
            scopes=record.scopes,
            status=OidcProviderStatus.DISABLED,
            created_by=record.created_by,
            created_at=record.created_at,
        )
        self._providers[provider_id] = disabled
        return disabled

    async def create_login_state(
        self,
        *,
        provider_id: UUID,
        state: str,
        nonce: str,
        code_verifier: str,
        redirect_uri: str,
        expires_at: datetime,
    ) -> None:
        self._states[state] = (
            provider_id,
            OidcLoginStateRecord(provider_id, nonce, code_verifier, redirect_uri),
            expires_at,
            None,
        )

    async def consume_login_state(
        self, state: str, provider_id: UUID, now: datetime
    ) -> OidcLoginStateRecord | None:
        entry = self._states.get(state)
        if entry is None:
            return None
        stored_provider_id, record, expires_at, consumed_at = entry
        if (
            stored_provider_id != provider_id
            or consumed_at is not None
            or expires_at <= now
        ):
            return None
        self._states[state] = (stored_provider_id, record, expires_at, now)
        return record

    async def append_audit(
        self,
        *,
        actor_id: UUID | None,
        action: str,
        result: str,
        request_id: str,
        context: dict[str, str],
    ) -> None:
        del actor_id, result, request_id, context
        self.audit_actions.append(action)
