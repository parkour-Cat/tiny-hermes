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
from collections.abc import Callable
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


class OutboundJwksKeySource:
    """The one caller of `SafeOutboundClient` this feature needs."""

    def __init__(self, client: Callable[[], SafeOutboundClient]) -> None:
        # A factory, not an instance: `ApplicationResources.outbound_client`'s
        # own docstring explains why — a client that outlives one request is a
        # client whose approved egress route was read at a different time
        # than it is used.
        self._client = client

    async def resolve(
        self, *, public_key: str | None, jwks_url: str | None, token: str
    ) -> str | None:
        if public_key is not None:
            return public_key
        if jwks_url is None:
            return None
        kid = _unverified_kid(token)
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
        matched = _matching_jwk(cast(list[Any], keys), kid)
        return None if matched is None else jwk_to_pem(matched)


def _unverified_kid(token: str) -> str | None:
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError:
        return None
    return kid if isinstance(kid, str) else None
