"""Turning a `channel_issuers` row into the one PEM key `verify` checks a
signature against.

Two things are tested separately on purpose. `jwk_to_pem` is pure — a JWK
member in, a PEM key out, or `None` for a key type this platform's allow-list
(RS256/ES256 only) will never check anything against — so it needs no
network double. `OutboundJwksKeySource.resolve` is the orchestration around
it: which key a JWKS document's `kid`s pick out, and what happens when the
fetch itself fails. It is exercised against a fake shaped like
`SafeOutboundClient`'s call surface rather than the real thing, the same way
`OutboundTarballSource`'s unit-level behaviour is proven without a socket in
its own module — the real network path belongs to an integration test.
"""

import json
from dataclasses import dataclass, field
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm
from tiny_hermes.identity.infrastructure.jwks_key_source import (
    JwksCache,
    OutboundJwksKeySource,
    jwk_to_pem,
)
from tiny_hermes.outbound.errors import OutboundUnreachable


def _rsa_jwk(kid: str) -> tuple[str, dict[str, Any]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    jwk = json.loads(RSAAlgorithm(RSAAlgorithm.SHA256).to_jwk(private_key.public_key()))
    jwk["kid"] = kid
    jwk["alg"] = "RS256"
    return public_pem, jwk


def _ec_jwk(kid: str) -> tuple[str, dict[str, Any]]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    jwk = json.loads(ECAlgorithm(ECAlgorithm.SHA256).to_jwk(private_key.public_key()))
    jwk["kid"] = kid
    jwk["alg"] = "ES256"
    return public_pem, jwk


# -- jwk_to_pem, pure -------------------------------------------------------


def test_an_rsa_jwk_becomes_a_pem_key_usable_to_verify() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm(RSAAlgorithm.SHA256).to_jwk(private_key.public_key()))
    token = jwt.encode({"sub": "x"}, private_key, algorithm="RS256")

    pem = jwk_to_pem(jwk)

    assert pem is not None
    assert jwt.decode(token, key=pem, algorithms=["RS256"]) == {"sub": "x"}


def test_an_ec_jwk_becomes_a_pem_key_usable_to_verify() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(ECAlgorithm(ECAlgorithm.SHA256).to_jwk(private_key.public_key()))
    token = jwt.encode({"sub": "x"}, private_key, algorithm="ES256")

    pem = jwk_to_pem(jwk)

    assert pem is not None
    assert jwt.decode(token, key=pem, algorithms=["ES256"]) == {"sub": "x"}


def test_a_jwk_of_an_unsupported_key_type_yields_no_pem() -> None:
    assert jwk_to_pem({"kty": "oct", "k": "c2VjcmV0"}) is None


def test_a_malformed_jwk_yields_no_pem_rather_than_raising() -> None:
    assert jwk_to_pem({"kty": "RSA", "n": "not-base64!!", "e": "AQAB"}) is None


# -- OutboundJwksKeySource.resolve, against a fake transport ---------------


@dataclass
class FakeResponse:
    status_code: int
    body: dict[str, Any] | None = None

    def json(self) -> Any:
        return self.body


@dataclass
class FakeOutboundClient:
    """Shaped like `SafeOutboundClient`'s call surface — an async context
    manager with `.request(method, url)` — and nothing more, matching what
    `OutboundJwksKeySource` actually calls."""

    response: FakeResponse | None = None
    error: Exception | None = None
    requested: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    async def __aenter__(self) -> "FakeOutboundClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        del exc_info

    async def request(self, method: str, url: str) -> FakeResponse:
        self.requested.append((method, url))
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def _jwks_body(*jwks: dict[str, Any]) -> dict[str, Any]:
    return {"keys": list(jwks)}


async def test_public_key_is_returned_directly_without_any_fetch() -> None:
    client = FakeOutboundClient()
    source = OutboundJwksKeySource(lambda: client)  # type: ignore[arg-type]

    result = await source.resolve(public_key="a-pem-key", jwks_url=None, token="irrelevant")

    assert result == "a-pem-key"
    assert client.requested == []


async def test_a_jwks_url_is_fetched_and_the_matching_kid_is_returned() -> None:
    pem, jwk = _rsa_jwk("key-1")
    client = FakeOutboundClient(response=FakeResponse(200, _jwks_body(jwk)))
    source = OutboundJwksKeySource(lambda: client)  # type: ignore[arg-type]
    token = jwt.encode({}, "", algorithm="none", headers={"kid": "key-1"})

    result = await source.resolve(
        public_key=None, jwks_url="https://idp.acme.example/jwks.json", token=token
    )

    assert result == pem
    assert client.requested == [("GET", "https://idp.acme.example/jwks.json")]


async def test_a_kid_that_matches_nothing_in_the_set_resolves_to_no_key() -> None:
    _, jwk = _rsa_jwk("key-1")
    client = FakeOutboundClient(response=FakeResponse(200, _jwks_body(jwk)))
    source = OutboundJwksKeySource(lambda: client)  # type: ignore[arg-type]
    token = jwt.encode({}, "", algorithm="none", headers={"kid": "key-2"})

    result = await source.resolve(
        public_key=None, jwks_url="https://idp.acme.example/jwks.json", token=token
    )

    assert result is None


async def test_a_lone_key_is_used_when_the_token_names_no_kid() -> None:
    pem, jwk = _ec_jwk("only-key")
    del jwk["kid"]
    client = FakeOutboundClient(response=FakeResponse(200, _jwks_body(jwk)))
    source = OutboundJwksKeySource(lambda: client)  # type: ignore[arg-type]
    token = jwt.encode({}, "", algorithm="none")

    result = await source.resolve(
        public_key=None, jwks_url="https://idp.acme.example/jwks.json", token=token
    )

    assert result == pem


async def test_an_unlabelled_token_against_a_multi_key_set_resolves_to_no_key() -> None:
    """Picking among several keys without a `kid` to disambiguate would be
    guessing which one signed the token — refused rather than guessed."""
    _, jwk_a = _rsa_jwk("a")
    del jwk_a["kid"]
    _, jwk_b = _rsa_jwk("b")
    del jwk_b["kid"]
    client = FakeOutboundClient(response=FakeResponse(200, _jwks_body(jwk_a, jwk_b)))
    source = OutboundJwksKeySource(lambda: client)  # type: ignore[arg-type]
    token = jwt.encode({}, "", algorithm="none")

    result = await source.resolve(
        public_key=None, jwks_url="https://idp.acme.example/jwks.json", token=token
    )

    assert result is None


async def test_a_non_200_response_resolves_to_no_key() -> None:
    client = FakeOutboundClient(response=FakeResponse(500, None))
    source = OutboundJwksKeySource(lambda: client)  # type: ignore[arg-type]

    result = await source.resolve(
        public_key=None, jwks_url="https://idp.acme.example/jwks.json", token="x.y.z"
    )

    assert result is None


async def test_an_unreachable_jwks_endpoint_resolves_to_no_key_rather_than_raising() -> None:
    client = FakeOutboundClient(error=OutboundUnreachable("boom", effect_unknown=False))
    source = OutboundJwksKeySource(lambda: client)  # type: ignore[arg-type]

    result = await source.resolve(
        public_key=None, jwks_url="https://idp.acme.example/jwks.json", token="x.y.z"
    )

    assert result is None


async def test_a_malformed_jwks_document_resolves_to_no_key() -> None:
    client = FakeOutboundClient(response=FakeResponse(200, {"not": "a keyset"}))
    source = OutboundJwksKeySource(lambda: client)  # type: ignore[arg-type]

    result = await source.resolve(
        public_key=None, jwks_url="https://idp.acme.example/jwks.json", token="x.y.z"
    )

    assert result is None


# -- JwksCache: bounded and TTL'd (task-9 review finding E) -----------------
#
# Every credential exchange used to refetch the same JWKS document over the
# egress proxy — no cache, no rate limit, and the exchange route itself has
# no session to throttle against. A `jwks_url` is workspace-admin
# configuration, not attacker input, but a burst of exchanges (legitimate or
# not) still turned into a burst of outbound fetches to the same URL for no
# reason. `OutboundJwksKeySource.resolve` still goes through
# `SafeOutboundClient` on every *miss* — `test_jwks_fetch.py` is what proves
# that against a real egress proxy — this suite is only about when a miss
# happens at all.


class FakeClock:
    """A clock `JwksCache` can be driven by hand, so a TTL test does not
    need a real `sleep`."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_a_second_resolve_within_ttl_reuses_the_cached_document_without_fetching() -> None:
    pem, jwk = _rsa_jwk("key-1")
    client = FakeOutboundClient(response=FakeResponse(200, _jwks_body(jwk)))
    cache = JwksCache(ttl_seconds=300.0, clock=FakeClock())
    source = OutboundJwksKeySource(lambda: client, cache=cache)  # type: ignore[arg-type]
    token = jwt.encode({}, "", algorithm="none", headers={"kid": "key-1"})

    first = await source.resolve(
        public_key=None, jwks_url="https://idp.acme.example/jwks.json", token=token
    )
    second = await source.resolve(
        public_key=None, jwks_url="https://idp.acme.example/jwks.json", token=token
    )

    assert first == pem
    assert second == pem
    assert client.requested == [("GET", "https://idp.acme.example/jwks.json")]


async def test_a_resolve_after_the_ttl_expires_fetches_again() -> None:
    _pem, jwk = _rsa_jwk("key-1")
    client = FakeOutboundClient(response=FakeResponse(200, _jwks_body(jwk)))
    clock = FakeClock()
    cache = JwksCache(ttl_seconds=60.0, clock=clock)
    source = OutboundJwksKeySource(lambda: client, cache=cache)  # type: ignore[arg-type]
    token = jwt.encode({}, "", algorithm="none", headers={"kid": "key-1"})

    await source.resolve(
        public_key=None, jwks_url="https://idp.acme.example/jwks.json", token=token
    )
    clock.now += 61.0
    await source.resolve(
        public_key=None, jwks_url="https://idp.acme.example/jwks.json", token=token
    )

    assert client.requested == [
        ("GET", "https://idp.acme.example/jwks.json"),
        ("GET", "https://idp.acme.example/jwks.json"),
    ]


async def test_a_different_jwks_url_is_cached_and_fetched_separately() -> None:
    _pem_a, jwk_a = _rsa_jwk("key-a")
    _pem_b, jwk_b = _rsa_jwk("key-b")
    client_a = FakeOutboundClient(response=FakeResponse(200, _jwks_body(jwk_a)))
    client_b = FakeOutboundClient(response=FakeResponse(200, _jwks_body(jwk_b)))
    clients = {"https://a.example/jwks.json": client_a, "https://b.example/jwks.json": client_b}
    cache = JwksCache(clock=FakeClock())

    async def _resolve(url: str) -> str | None:
        source = OutboundJwksKeySource(lambda: clients[url], cache=cache)  # type: ignore[arg-type]
        token = jwt.encode(
            {}, "", algorithm="none", headers={"kid": "key-a" if "a." in url else "key-b"}
        )
        return await source.resolve(public_key=None, jwks_url=url, token=token)

    await _resolve("https://a.example/jwks.json")
    await _resolve("https://b.example/jwks.json")
    # Both still cached under their own url — refetching "a" again must not
    # touch "b"'s client.
    await _resolve("https://a.example/jwks.json")

    assert client_a.requested == [("GET", "https://a.example/jwks.json")]
    assert client_b.requested == [("GET", "https://b.example/jwks.json")]


def test_the_cache_is_bounded_and_evicts_the_entry_closest_to_expiry() -> None:
    clock = FakeClock()
    cache = JwksCache(ttl_seconds=300.0, max_entries=2, clock=clock)
    cache.put("https://a.example/jwks.json", [{"kid": "a"}])
    clock.now += 1.0
    cache.put("https://b.example/jwks.json", [{"kid": "b"}])
    clock.now += 1.0

    # A third distinct url over the bound evicts the one nearest expiry —
    # "a", put first and therefore expiring first — not an arbitrary one.
    cache.put("https://c.example/jwks.json", [{"kid": "c"}])

    assert cache.get("https://a.example/jwks.json") is None
    assert cache.get("https://b.example/jwks.json") == [{"kid": "b"}]
    assert cache.get("https://c.example/jwks.json") == [{"kid": "c"}]


def test_an_expired_entry_reads_back_as_a_miss() -> None:
    clock = FakeClock()
    cache = JwksCache(ttl_seconds=10.0, clock=clock)
    cache.put("https://a.example/jwks.json", [{"kid": "a"}])

    clock.now += 11.0

    assert cache.get("https://a.example/jwks.json") is None
