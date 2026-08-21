"""Turning a `channel_issuers` row into one PEM key `verify` can check a
signature against.

Design §3: a row carries a fixed public key or a JWKS URL, the issuer's
choice, not this platform's. A JWKS fetch is outbound traffic this process
did not initiate — the target is a URL a workspace administrator typed in —
so it goes through `SafeOutboundClient` exactly the way `OutboundTarballSource`
carries a skill import: through the one way out of this process, never a
client built here. `ApplicationResources.outbound_client` is the only place
that constructs one; this module receives a factory, the same shape
`OutboundTarballSource` takes.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from tiny_hermes.outbound.client import SafeOutboundClient
from tiny_hermes.outbound.errors import OutboundError


def jwk_to_pem(jwk: dict[str, Any]) -> str | None:
    """One JWK member to a PEM-encoded public key, or `None` if this platform
    does not know how to read it. `end_user_credential.ALLOWED_ALGORITHMS`
    accepts RS256 and ES256 only, so a `kty` outside `RSA`/`EC` — or one that
    fails to parse at all — is not a key this platform will ever check a
    signature against, and `None` says that the same way a missing `kid`
    match does further up the call chain: nothing to verify with, not an
    error to raise.
    """
    kty = jwk.get("kty")
    try:
        if kty == "RSA":
            public_key = cast(RSAPublicKey, RSAAlgorithm.from_jwk(json.dumps(jwk)))
        elif kty == "EC":
            public_key = cast(EllipticCurvePublicKey, ECAlgorithm.from_jwk(json.dumps(jwk)))
        else:
            return None
    except (jwt.InvalidKeyError, jwt.DecodeError, ValueError, TypeError):
        return None
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def _matching_jwk(keys: list[Any], kid: str | None) -> dict[str, Any] | None:
    """§4.1 names no `kid` requirement of its own, so this follows the JWKS
    spec's own convention: match by `kid` when the token names one. An
    unlabelled token may use a set's one and only key, but picking among
    several without a `kid` to disambiguate would be guessing which one
    signed it, so that resolves to no key rather than a guess."""
    candidates: list[dict[str, Any]] = [key for key in keys if isinstance(key, dict)]
    if kid is not None:
        for candidate in candidates:
            if candidate.get("kid") == kid:
                return candidate
        return None
    return candidates[0] if len(candidates) == 1 else None


@dataclass
class _CacheEntry:
    keys: list[Any]
    expires_at: float


class JwksCache:
    """Task-9 review finding E: a channel issuer's JWKS document, cached by
    its own `jwks_url`, so a burst of credential exchanges against the same
    issuer costs one outbound fetch rather than one per exchange. Bounded
    two ways: a TTL, so a rotated key is picked up again within
    `ttl_seconds` rather than held stale indefinitely, and an entry-count
    ceiling, since the number of distinct `jwks_url`s a workspace registers
    is admin-controlled but nothing stops it from growing without limit.

    Deliberately holds the raw `keys` list rather than a resolved PEM: a
    JWKS document can carry several keys distinguished by `kid`, and which
    one a given token needs is a question of that token, not of the
    document — caching post-`_matching_jwk` would mean one cache slot per
    `(url, kid)` for what is really one fetch.

    `clock` is injectable so a TTL is testable without a real `sleep` — the
    same reasoning most of this codebase's TTL-bearing code follows.
    """

    def __init__(
        self,
        ttl_seconds: float = 300.0,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: dict[str, _CacheEntry] = {}

    def get(self, jwks_url: str) -> list[Any] | None:
        entry = self._entries.get(jwks_url)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            del self._entries[jwks_url]
            return None
        return entry.keys

    def put(self, jwks_url: str, keys: list[Any]) -> None:
        if jwks_url not in self._entries and len(self._entries) >= self._max_entries:
            # Evict whichever entry is closest to expiring anyway, rather
            # than an arbitrary one — cheap at this bound (linear in
            # `max_entries`, a few hundred at most) and it keeps the
            # freshest keys around the longest.
            oldest = min(self._entries, key=lambda key: self._entries[key].expires_at)
            del self._entries[oldest]
        self._entries[jwks_url] = _CacheEntry(
            keys=keys, expires_at=self._clock() + self._ttl_seconds
        )


class OutboundJwksKeySource:
    """The one caller of `SafeOutboundClient` this feature needs."""

    def __init__(
        self,
        client: Callable[[], SafeOutboundClient],
        cache: JwksCache | None = None,
    ) -> None:
        # A factory, not an instance: `ApplicationResources.outbound_client`'s
        # own docstring explains why — a client that outlives one request is a
        # client whose approved egress route was read at a different time
        # than it is used.
        self._client = client
        # `None` by default so every existing caller — the fakes in this
        # module's own tests, `test_jwks_fetch.py`'s real-proxy suite — keeps
        # meaning exactly what it always did: fetch every time. Task-9
        # review finding E is only wired at the one place a cache can live
        # longer than a single request: `ApplicationResources`.
        self._cache = cache

    async def resolve(
        self, *, public_key: str | None, jwks_url: str | None, token: str
    ) -> str | None:
        if public_key is not None:
            return public_key
        if jwks_url is None:
            return None
        kid = _unverified_kid(token)
        keys = self._cache.get(jwks_url) if self._cache is not None else None
        if keys is None:
            keys = await self._fetch(jwks_url)
            if keys is None:
                return None
            if self._cache is not None:
                self._cache.put(jwks_url, keys)
        matched = _matching_jwk(keys, kid)
        return None if matched is None else jwk_to_pem(matched)

    async def _fetch(self, jwks_url: str) -> list[Any] | None:
        try:
            async with self._client() as client:
                response = await client.request("GET", jwks_url)
        except OutboundError:
            return None
        if response.status_code != 200:
            return None
        try:
            body = response.json()
            keys = body["keys"]
        except (ValueError, KeyError, TypeError):
            return None
        if not isinstance(keys, list):
            return None
        return cast(list[Any], keys)


def _unverified_kid(token: str) -> str | None:
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError:
        return None
    return kid if isinstance(kid, str) else None
