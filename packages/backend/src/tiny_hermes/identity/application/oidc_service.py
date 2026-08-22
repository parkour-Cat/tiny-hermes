"""Orchestrating OIDC login design §1 (provider registration) and §2 (the
Authorization Code + PKCE flow) around the pieces that already exist:
`AuthService` mints the session (the same code path a local login uses —
`AuthService.issue_session_for`), `CredentialResolver` resolves the client
secret (never a plaintext column — see `OidcProviderRow`'s own docstring),
and `OutboundJwksKeySource` resolves the signing key (reused as-is from the
end-user identity feature).

**Every failure past a successful `state` lookup collapses to one refusal.**
Bad nonce, wrong `aud`, expired, bad signature, a token endpoint that
answered with garbage — `OidcLoginRefused` covers all of them, for the same
reason `end_user_credential`'s module docstring gives: an attacker driving
this callback must not be able to fingerprint *which* check failed. A
disabled or unregistered provider and a network failure reaching the IdP are
kept distinguishable from that (`OidcProviderNotUsable`,
`OidcProviderUnreachable`) because neither leaks anything about a login
attempt in progress — they are true before any `code`/`state` is presented
at all.
"""

import secrets
from base64 import urlsafe_b64encode
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, cast
from urllib.parse import urlencode
from uuid import UUID

from tiny_hermes.identity.application.auth_service import AuthService
from tiny_hermes.identity.domain.models import AuthenticatedUser, OidcProviderStatus
from tiny_hermes.identity.domain.oidc import OidcRefusal, verify_id_token
from tiny_hermes.identity.infrastructure.jwks_key_source import OutboundJwksKeySource
from tiny_hermes.identity.infrastructure.oidc_discovery import (
    DiscoveryDocument,
    OutboundDiscoveryFetcher,
)
from tiny_hermes.identity.ports.oidc_store import OidcProviderRecord, OidcProviderStore
from tiny_hermes.model_catalog.infrastructure.credentials import (
    CredentialMissing,
    CredentialResolver,
)
from tiny_hermes.outbound.client import SafeOutboundClient
from tiny_hermes.outbound.errors import OutboundError
from tiny_hermes.secrets.ports.store import SecretStore
from tiny_hermes.tenancy.domain.models import Actor

#: How long a `state`/`nonce`/PKCE verifier stays redeemable. Long enough for
#: a real IdP's own login UI, short enough that a leaked, unused `state`
#: cannot be replayed indefinitely.
LOGIN_STATE_TTL = timedelta(minutes=10)

#: RFC 7636's own bound. `secrets.token_urlsafe(N)` yields roughly `4N/3`
#: characters; 48 lands inside [43, 128] with room to spare.
CODE_VERIFIER_BYTES = 48


class ForbiddenOidcAction(Exception):
    pass


class InvalidOidcProvider(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnknownOidcProvider(Exception):
    pass


class OidcProviderNotUsable(Exception):
    """Missing or disabled — collapsed to one case so a login attempt cannot
    use the response to enumerate which provider ids exist."""


class OidcProviderUnreachable(Exception):
    """Discovery, JWKS, or the token endpoint did not answer, or the
    discovery document's own `issuer` did not match what was registered."""


class OidcLoginRefused(Exception):
    """Bad `state`, bad `nonce`, bad signature, wrong claims, or a token
    endpoint that refused the exchange — see the module docstring for why
    these are one exception rather than several."""


@dataclass(frozen=True)
class AuthorizationRedirect:
    url: str


@dataclass(frozen=True)
class OidcLoginResult:
    session_token: str
    csrf_token: str
    user: AuthenticatedUser


class OidcProviderService:
    def __init__(
        self,
        store: OidcProviderStore,
        secrets_store: SecretStore | None,
        kek: bytes | None,
        discovery: OutboundDiscoveryFetcher,
        jwks: OutboundJwksKeySource,
        client_factory: Callable[[], SafeOutboundClient],
        auth: AuthService,
    ) -> None:
        self._store = store
        self._credentials = CredentialResolver(secrets_store, kek)
        self._discovery = discovery
        self._jwks = jwks
        self._client_factory = client_factory
        self._auth = auth

    # -- §1: provider configuration ---------------------------------------

    async def register(
        self,
        actor: Actor,
        *,
        issuer: str,
        client_id: str,
        client_secret_ref: str,
        discovery_url: str,
        scopes: Sequence[str],
        request_id: str,
    ) -> OidcProviderRecord:
        await self._require_admin(actor)
        normalized_issuer = issuer.strip()
        normalized_client_id = client_id.strip()
        normalized_ref = client_secret_ref.strip()
        normalized_discovery = discovery_url.strip()
        if not normalized_issuer or not normalized_client_id or not normalized_ref:
            raise InvalidOidcProvider("issuer, client_id and client_secret_ref are required")
        if not normalized_discovery.startswith(("http://", "https://")):
            raise InvalidOidcProvider("discovery_url must be an http(s) URL")
        record = await self._store.create_provider(
            issuer=normalized_issuer,
            client_id=normalized_client_id,
            client_secret_ref=normalized_ref,
            discovery_url=normalized_discovery,
            scopes=tuple(scopes) or ("openid",),
            created_by=actor.id,
        )
        await self._store.append_audit(
            actor_id=actor.id,
            action="identity.oidc_provider_registered",
            result="succeeded",
            request_id=request_id,
            context={"issuer": normalized_issuer},
        )
        return record

    async def list_providers(self, actor: Actor) -> Sequence[OidcProviderRecord]:
        await self._require_admin(actor)
        return await self._store.list_providers()

    async def disable(
        self, actor: Actor, provider_id: UUID, request_id: str
    ) -> OidcProviderRecord:
        await self._require_admin(actor)
        record = await self._store.disable_provider(provider_id)
        if record is None:
            raise UnknownOidcProvider
        await self._store.append_audit(
            actor_id=actor.id,
            action="identity.oidc_provider_disabled",
            result="succeeded",
            request_id=request_id,
            context={},
        )
        return record

    # -- §2: the flow -------------------------------------------------------

    async def start(self, provider_id: UUID, redirect_uri: str) -> AuthorizationRedirect:
        provider = await self._active_provider(provider_id)
        document = await self._fetch_discovery(provider)
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(CODE_VERIFIER_BYTES)
        now = datetime.now(UTC)
        # Server-side, not a cookie (design §2's own line) — a callback can
        # only be completed with what this process itself remembers issuing.
        await self._store.create_login_state(
            provider_id=provider.id,
            state=state,
            nonce=nonce,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            expires_at=now + LOGIN_STATE_TTL,
        )
        params = {
            "response_type": "code",
            "client_id": provider.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(provider.scopes) or "openid",
            "state": state,
            "nonce": nonce,
            "code_challenge": _code_challenge(verifier),
            "code_challenge_method": "S256",
        }
        return AuthorizationRedirect(f"{document.authorization_endpoint}?{urlencode(params)}")

    async def handle_callback(
        self, provider_id: UUID, *, code: str, state: str, request_id: str
    ) -> OidcLoginResult:
        provider = await self._active_provider(provider_id)
        now = datetime.now(UTC)
        # Single-use, consumed atomically on first presentation
        # (`SqlOidcProviderStore.consume_login_state`) — a replayed `state`
        # finds no row here, indistinguishable from one that never existed.
        login_state = await self._store.consume_login_state(state, provider.id, now)
        if login_state is None:
            raise OidcLoginRefused("state")
        document = await self._fetch_discovery(provider)
        token_response = await self._exchange_code(
            document.token_endpoint,
            provider,
            code,
            login_state.redirect_uri,
            login_state.code_verifier,
        )
        id_token = token_response.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise OidcLoginRefused("token_response")
        key = await self._jwks.resolve(public_key=None, jwks_url=document.jwks_uri, token=id_token)
        if key is None:
            raise OidcLoginRefused("key")
        verified = verify_id_token(
            id_token,
            key_pem=key,
            issuer=provider.issuer,
            client_id=provider.client_id,
            nonce=login_state.nonce,
        )
        if isinstance(verified, OidcRefusal):
            raise OidcLoginRefused(verified.reason)
        # OIDC login design's red line: looked up (and, on a miss, created)
        # by `sub` alone — see `AuthService.find_or_create_oidc_identity`'s
        # own docstring for why this never touches a local identity's email.
        display_name = verified.display_name or verified.subject
        user = await self._auth.find_or_create_oidc_identity(
            verified.subject, display_name, request_id
        )
        session_token, csrf_token = await self._auth.issue_session_for(
            user, request_id, "identity.oidc_login_succeeded"
        )
        return OidcLoginResult(session_token, csrf_token, user)

    # -- internals ------------------------------------------------------

    async def _active_provider(self, provider_id: UUID) -> OidcProviderRecord:
        record = await self._store.get_provider(provider_id)
        if record is None or record.status is not OidcProviderStatus.ACTIVE:
            raise OidcProviderNotUsable
        return record

    async def _fetch_discovery(self, provider: OidcProviderRecord) -> DiscoveryDocument:
        document = await self._discovery.fetch(provider.discovery_url)
        if document is None:
            raise OidcProviderUnreachable
        if document.issuer != provider.issuer:
            # OIDC Core §4.3: the discovery document's own `issuer` must
            # match the URL it was fetched for. A mismatch means the
            # `discovery_url` this provider was registered with no longer
            # points at the IdP it was configured for — treated as
            # unreachable rather than silently trusting a different issuer.
            raise OidcProviderUnreachable
        return document

    async def _exchange_code(
        self,
        token_endpoint: str,
        provider: OidcProviderRecord,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> dict[str, Any]:
        try:
            client_secret = await self._credentials.resolve(provider.client_secret_ref)
        except CredentialMissing as missing:
            raise OidcProviderUnreachable from missing
        body = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": provider.client_id,
            "client_secret": client_secret,
            "code_verifier": code_verifier,
        }
        try:
            async with self._client_factory() as client:
                response = await client.request(
                    "POST",
                    token_endpoint,
                    data=body,
                    headers={"Accept": "application/json"},
                )
        except OutboundError as failed:
            raise OidcProviderUnreachable from failed
        if response.status_code != 200:
            raise OidcLoginRefused("token_exchange")
        try:
            parsed = response.json()
        except ValueError as error:
            raise OidcLoginRefused("token_response") from error
        if not isinstance(parsed, dict):
            raise OidcLoginRefused("token_response")
        return cast(dict[str, Any], parsed)

    async def _require_admin(self, actor: Actor) -> None:
        if actor.is_service_account or not actor.is_platform_admin:
            raise ForbiddenOidcAction


def _code_challenge(verifier: str) -> str:
    digest = sha256(verifier.encode("ascii")).digest()
    return urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
