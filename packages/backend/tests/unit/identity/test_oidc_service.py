"""`OidcProviderService`: orchestrating registration (§1) and the
Authorization Code + PKCE flow (§2) without a database or a real IdP.

Real signed tokens throughout, the same discipline `test_end_user_service.py`
uses for `EndUserIdentityService` — what is new at this layer, past
`verify_id_token`'s own exhaustive suite in `test_oidc_domain.py`, is
`state`/`nonce`/PKCE bookkeeping, provider status, and — the one thing this
whole feature exists to get right — that a login never reaches an existing
User by matching its `email` claim.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from tiny_hermes.identity.application.auth_service import AuthService
from tiny_hermes.identity.application.oidc_service import (
    ForbiddenOidcAction,
    InvalidOidcProvider,
    OidcLoginRefused,
    OidcProviderNotUsable,
    OidcProviderService,
    OidcProviderUnreachable,
)
from tiny_hermes.identity.infrastructure.jwks_key_source import OutboundJwksKeySource
from tiny_hermes.identity.infrastructure.memory_oidc_store import MemoryOidcProviderStore
from tiny_hermes.identity.infrastructure.memory_store import MemoryAuthStore
from tiny_hermes.identity.infrastructure.oidc_discovery import OutboundDiscoveryFetcher
from tiny_hermes.outbound.errors import OutboundUnreachable
from tiny_hermes.tenancy.domain.models import Actor

ISSUER = "https://idp.acme.example"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
AUTHORIZATION_ENDPOINT = f"{ISSUER}/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/token"
JWKS_URI = f"{ISSUER}/jwks.json"
CLIENT_ID = "platform-console"
CLIENT_SECRET_REF = "OIDC_TEST_CLIENT_SECRET"  # noqa: S105 - an env var name, not a secret
REDIRECT_URI = "https://console.example/api/v1/auth/oidc/whatever/callback"

ADMIN = Actor(uuid4(), is_platform_admin=True)
NON_ADMIN = Actor(uuid4(), is_platform_admin=False)


def _rsa_keypair() -> tuple[Any, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_key, public_pem


PRIVATE_KEY, PUBLIC_PEM = _rsa_keypair()
_OTHER_PRIVATE_KEY, _OTHER_PUBLIC_PEM = _rsa_keypair()
JWK = json.loads(RSAAlgorithm(RSAAlgorithm.SHA256).to_jwk(PRIVATE_KEY.public_key()))
JWK["kid"] = "acme-2026"


def _id_token(*, nonce: str, key: Any = PRIVATE_KEY, **overrides: object) -> str:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "idp-user-1",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "nonce": nonce,
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "acme-2026"})


@dataclass
class FakeResponse:
    status_code: int
    body: Any = None

    def json(self) -> Any:
        return self.body


@dataclass
class FakeOutboundClient:
    """Shaped like `SafeOutboundClient`'s call surface, keyed by `(method,
    url)` — one instance is shared by the discovery fetcher, the JWKS
    source, and the service's own token-exchange POST, so a single fake
    stands in for the whole IdP."""

    responses: dict[tuple[str, str], FakeResponse] = field(
        default_factory=dict[tuple[str, str], FakeResponse]
    )
    unreachable: set[str] = field(default_factory=set[str])
    token_requests: list[dict[str, str]] = field(default_factory=list[dict[str, str]])

    async def __aenter__(self) -> "FakeOutboundClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        del exc_info

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FakeResponse:
        del json, headers
        if url in self.unreachable:
            raise OutboundUnreachable("boom", effect_unknown=False)
        if data is not None:
            self.token_requests.append(data)
        response = self.responses.get((method, url))
        assert response is not None, f"no fake response registered for {method} {url}"
        return response


def _default_client() -> FakeOutboundClient:
    client = FakeOutboundClient()
    client.responses[("GET", DISCOVERY_URL)] = FakeResponse(
        200,
        {
            "issuer": ISSUER,
            "authorization_endpoint": AUTHORIZATION_ENDPOINT,
            "token_endpoint": TOKEN_ENDPOINT,
            "jwks_uri": JWKS_URI,
        },
    )
    client.responses[("GET", JWKS_URI)] = FakeResponse(200, {"keys": [JWK]})
    return client


def _service(
    client: FakeOutboundClient, auth_store: MemoryAuthStore | None = None
) -> tuple[OidcProviderService, MemoryOidcProviderStore, MemoryAuthStore]:
    store = MemoryOidcProviderStore()
    auth = auth_store or MemoryAuthStore()
    service = OidcProviderService(
        store,
        secrets_store=None,
        kek=None,
        discovery=OutboundDiscoveryFetcher(lambda: client),  # type: ignore[arg-type]
        jwks=OutboundJwksKeySource(lambda: client),  # type: ignore[arg-type]
        client_factory=lambda: client,  # type: ignore[arg-type, return-value]
        auth=AuthService(auth, bootstrap_token="a" * 32, session_ttl_seconds=28_800),
    )
    return service, store, auth


async def _registered_provider(
    service: OidcProviderService, monkeypatch: pytest.MonkeyPatch
) -> Any:
    monkeypatch.setenv(CLIENT_SECRET_REF, "shhh-its-a-secret")  # noqa: S105
    return await service.register(
        ADMIN,
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret_ref=CLIENT_SECRET_REF,
        discovery_url=DISCOVERY_URL,
        scopes=["openid", "email"],
        request_id="req-register",
    )


def _state_from_redirect(url: str) -> str:
    query = parse_qs(urlsplit(url).query)
    return query["state"][0]


# -- §1: registration ---------------------------------------------------


async def test_register_requires_platform_admin() -> None:
    service, _, _ = _service(_default_client())

    with pytest.raises(ForbiddenOidcAction):
        await service.register(
            NON_ADMIN,
            issuer=ISSUER,
            client_id=CLIENT_ID,
            client_secret_ref=CLIENT_SECRET_REF,
            discovery_url=DISCOVERY_URL,
            scopes=[],
            request_id="req-1",
        )


async def test_register_rejects_a_non_http_discovery_url() -> None:
    service, _, _ = _service(_default_client())

    with pytest.raises(InvalidOidcProvider):
        await service.register(
            ADMIN,
            issuer=ISSUER,
            client_id=CLIENT_ID,
            client_secret_ref=CLIENT_SECRET_REF,
            discovery_url="not-a-url",
            scopes=[],
            request_id="req-1",
        )


# -- §1's own test: a disabled provider cannot start a new login --------


async def test_a_disabled_provider_cannot_start_a_new_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _default_client()
    service, store, _ = _service(client)
    provider = await _registered_provider(service, monkeypatch)
    await service.disable(ADMIN, provider.id, "req-disable")

    with pytest.raises(OidcProviderNotUsable):
        await service.start(provider.id, REDIRECT_URI)

    assert store.audit_actions[-1] == "identity.oidc_provider_disabled"


async def test_an_unknown_provider_cannot_start_a_login() -> None:
    service, _, _ = _service(_default_client())

    with pytest.raises(OidcProviderNotUsable):
        await service.start(uuid4(), REDIRECT_URI)


async def test_a_disabled_provider_cannot_complete_a_callback_either(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _default_client()
    service, _, _ = _service(client)
    provider = await _registered_provider(service, monkeypatch)
    await service.start(provider.id, REDIRECT_URI)
    await service.disable(ADMIN, provider.id, "req-disable")

    with pytest.raises(OidcProviderNotUsable):
        await service.handle_callback(
            provider.id, code="irrelevant", state="irrelevant", request_id="req-cb"
        )


# -- §2: the flow ---------------------------------------------------------


async def test_start_redirects_with_pkce_state_and_nonce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _default_client()
    service, _, _ = _service(client)
    provider = await _registered_provider(service, monkeypatch)

    redirect = await service.start(provider.id, REDIRECT_URI)

    parsed = urlsplit(redirect.url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == AUTHORIZATION_ENDPOINT
    query = parse_qs(parsed.query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["code_challenge_method"] == ["S256"]
    assert "state" in query and "nonce" in query and "code_challenge" in query


async def test_a_full_login_creates_a_new_user_then_reuses_it_on_a_second_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _default_client()
    service, _, auth_store = _service(client)
    provider = await _registered_provider(service, monkeypatch)

    first_redirect = await service.start(provider.id, REDIRECT_URI)
    state = _state_from_redirect(first_redirect.url)
    nonce = parse_qs(urlsplit(first_redirect.url).query)["nonce"][0]
    client.responses[("POST", TOKEN_ENDPOINT)] = FakeResponse(
        200, {"id_token": _id_token(nonce=nonce)}
    )

    first = await service.handle_callback(
        provider.id, code="auth-code-1", state=state, request_id="req-1"
    )

    assert first.user.subject == "idp-user-1"
    assert await auth_store.find_oidc_identity("idp-user-1") is not None
    # PKCE: the verifier minted at `/start` reached the token endpoint.
    assert client.token_requests[-1]["code_verifier"]
    assert client.token_requests[-1]["client_secret"] == "shhh-its-a-secret"  # noqa: S105

    second_redirect = await service.start(provider.id, REDIRECT_URI)
    second_state = _state_from_redirect(second_redirect.url)
    second_nonce = parse_qs(urlsplit(second_redirect.url).query)["nonce"][0]
    client.responses[("POST", TOKEN_ENDPOINT)] = FakeResponse(
        200, {"id_token": _id_token(nonce=second_nonce)}
    )

    second = await service.handle_callback(
        provider.id, code="auth-code-2", state=second_state, request_id="req-2"
    )

    assert second.user.id == first.user.id


async def test_an_unknown_state_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, _ = _service(_default_client())
    provider = await _registered_provider(service, monkeypatch)

    with pytest.raises(OidcLoginRefused):
        await service.handle_callback(
            provider.id, code="auth-code", state="a-state-nobody-issued", request_id="req-1"
        )


async def test_a_replayed_state_is_refused_the_second_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _default_client()
    service, _, _ = _service(client)
    provider = await _registered_provider(service, monkeypatch)
    redirect = await service.start(provider.id, REDIRECT_URI)
    state = _state_from_redirect(redirect.url)
    nonce = parse_qs(urlsplit(redirect.url).query)["nonce"][0]
    client.responses[("POST", TOKEN_ENDPOINT)] = FakeResponse(
        200, {"id_token": _id_token(nonce=nonce)}
    )

    await service.handle_callback(provider.id, code="auth-code", state=state, request_id="req-1")

    with pytest.raises(OidcLoginRefused):
        await service.handle_callback(
            provider.id, code="auth-code", state=state, request_id="req-2"
        )


async def test_a_bad_nonce_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _default_client()
    service, _, _ = _service(client)
    provider = await _registered_provider(service, monkeypatch)
    redirect = await service.start(provider.id, REDIRECT_URI)
    state = _state_from_redirect(redirect.url)
    client.responses[("POST", TOKEN_ENDPOINT)] = FakeResponse(
        200, {"id_token": _id_token(nonce="a-nonce-this-login-never-issued")}
    )

    with pytest.raises(OidcLoginRefused):
        await service.handle_callback(
            provider.id, code="auth-code", state=state, request_id="req-1"
        )


async def test_a_bad_signature_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _default_client()
    service, _, _ = _service(client)
    provider = await _registered_provider(service, monkeypatch)
    redirect = await service.start(provider.id, REDIRECT_URI)
    state = _state_from_redirect(redirect.url)
    nonce = parse_qs(urlsplit(redirect.url).query)["nonce"][0]
    client.responses[("POST", TOKEN_ENDPOINT)] = FakeResponse(
        200, {"id_token": _id_token(nonce=nonce, key=_OTHER_PRIVATE_KEY)}
    )

    with pytest.raises(OidcLoginRefused):
        await service.handle_callback(
            provider.id, code="auth-code", state=state, request_id="req-1"
        )


async def test_an_expired_id_token_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _default_client()
    service, _, _ = _service(client)
    provider = await _registered_provider(service, monkeypatch)
    redirect = await service.start(provider.id, REDIRECT_URI)
    state = _state_from_redirect(redirect.url)
    nonce = parse_qs(urlsplit(redirect.url).query)["nonce"][0]
    now = datetime.now(UTC)
    expired = _id_token(
        nonce=nonce,
        iat=int((now - timedelta(minutes=20)).timestamp()),
        exp=int((now - timedelta(minutes=10)).timestamp()),
    )
    client.responses[("POST", TOKEN_ENDPOINT)] = FakeResponse(200, {"id_token": expired})

    with pytest.raises(OidcLoginRefused):
        await service.handle_callback(
            provider.id, code="auth-code", state=state, request_id="req-1"
        )


async def test_a_wrong_audience_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _default_client()
    service, _, _ = _service(client)
    provider = await _registered_provider(service, monkeypatch)
    redirect = await service.start(provider.id, REDIRECT_URI)
    state = _state_from_redirect(redirect.url)
    nonce = parse_qs(urlsplit(redirect.url).query)["nonce"][0]
    client.responses[("POST", TOKEN_ENDPOINT)] = FakeResponse(
        200, {"id_token": _id_token(nonce=nonce, aud="somebody-elses-client")}
    )

    with pytest.raises(OidcLoginRefused):
        await service.handle_callback(
            provider.id, code="auth-code", state=state, request_id="req-1"
        )


async def test_a_token_endpoint_error_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _default_client()
    service, _, _ = _service(client)
    provider = await _registered_provider(service, monkeypatch)
    redirect = await service.start(provider.id, REDIRECT_URI)
    state = _state_from_redirect(redirect.url)
    client.responses[("POST", TOKEN_ENDPOINT)] = FakeResponse(400, {"error": "invalid_grant"})

    with pytest.raises(OidcLoginRefused):
        await service.handle_callback(
            provider.id, code="auth-code", state=state, request_id="req-1"
        )


async def test_an_unreachable_discovery_endpoint_is_reported_as_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _default_client()
    client.unreachable.add(DISCOVERY_URL)
    service, _, _ = _service(client)
    provider = await _registered_provider(service, monkeypatch)

    with pytest.raises(OidcProviderUnreachable):
        await service.start(provider.id, REDIRECT_URI)


# -- the red line: never link an OIDC login to an existing user by email --


async def test_oidc_login_never_links_to_an_existing_local_user_by_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is the whole point of the feature. A local user already owns
    `alice@example.com`; an OIDC login whose `email` claim is the exact same
    address must still create — and sign in as — a different User. Nothing
    about this test should need the identity code to special-case it: the
    guarantee comes from `find_or_create_oidc_identity` looking up by `sub`
    alone, never by email, so this is here to catch a regression that adds
    an email-based shortcut later, not to prove today's code needs one."""
    client = _default_client()
    service, _, auth_store = _service(client)
    provider = await _registered_provider(service, monkeypatch)
    local_user = auth_store.seed_local_identity("alice@example.com", password_hash="a-real-hash")

    redirect = await service.start(provider.id, REDIRECT_URI)
    state = _state_from_redirect(redirect.url)
    nonce = parse_qs(urlsplit(redirect.url).query)["nonce"][0]
    client.responses[("POST", TOKEN_ENDPOINT)] = FakeResponse(
        200,
        {
            "id_token": _id_token(
                nonce=nonce, sub="idp-alice-does-not-know-about-the-local-account",
                email="alice@example.com",
            )
        },
    )

    result = await service.handle_callback(
        provider.id, code="auth-code", state=state, request_id="req-1"
    )

    assert result.user.id != local_user.id
