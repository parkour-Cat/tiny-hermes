"""Turning an OIDC provider's `discovery_url` into the three endpoints the
login flow needs.

OIDC login design §1: "discovery 文档 ... 走 egress-proxy" — a discovery fetch
is outbound traffic this process did not initiate (the target is a URL a
platform administrator typed in when registering the provider), so it goes
through `SafeOutboundClient` exactly the way `OutboundJwksKeySource` already
does for the sibling JWKS fetch. `ApplicationResources.outbound_client` is
the only place that constructs one; this module receives a factory, the same
shape `OutboundJwksKeySource` and `OutboundTarballSource` both take.

No cache here, unlike `JwksCache`: a JWKS is fetched once per credential
exchange on the end-user path, which is the high-volume side of this
platform; an OIDC login is a platform member signing in, orders of magnitude
rarer, and the extra round trip buys freshness on a document an
administrator can change without this process restarting.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from tiny_hermes.outbound.client import SafeOutboundClient
from tiny_hermes.outbound.errors import OutboundError


@dataclass(frozen=True)
class DiscoveryDocument:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


class OutboundDiscoveryFetcher:
    """The one caller of `SafeOutboundClient` this file needs."""

    def __init__(self, client: Callable[[], SafeOutboundClient]) -> None:
        self._client = client

    async def fetch(self, discovery_url: str) -> DiscoveryDocument | None:
        try:
            async with self._client() as client:
                response = await client.request("GET", discovery_url)
        except OutboundError:
            return None
        if response.status_code != 200:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        return _document(cast(dict[str, Any], body))


def _document(body: dict[str, Any]) -> DiscoveryDocument | None:
    fields = (
        body.get("issuer"),
        body.get("authorization_endpoint"),
        body.get("token_endpoint"),
        body.get("jwks_uri"),
    )
    if not all(isinstance(field, str) and field for field in fields):
        return None
    issuer, authorization_endpoint, token_endpoint, jwks_uri = fields
    return DiscoveryDocument(
        issuer=cast(str, issuer),
        authorization_endpoint=cast(str, authorization_endpoint),
        token_endpoint=cast(str, token_endpoint),
        jwks_uri=cast(str, jwks_uri),
    )
