"""`OutboundJwksKeySource` against a real socket, through a real egress proxy.

`test_jwks_key_source.py` proves the resolution logic — kid matching, the
lone-key fallback, malformed responses — against a fake shaped like
`SafeOutboundClient`'s call surface. What that cannot prove is the one thing
the brief calls out by name: that a `channel_issuers` row's `jwks_url` is
actually fetched *through* the egress proxy rather than by some other path.
This is the same technique `test_tarball_import.py` uses for
`OutboundTarballSource` — a real ASGI stand-in server, a real `EgressProxy`,
and the production client wired to both.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from tiny_hermes.identity.infrastructure.jwks_key_source import (
    JwksCache,
    OutboundJwksKeySource,
)
from tiny_hermes.outbound.client import EgressRoute, SafeOutboundClient

from ..egress_support import PROXY_TOKEN, ProxyHandle, running_proxy


@dataclass
class JwksHost:
    """A server that answers with whatever JWKS document the test handed it."""

    body: bytes = b"{}"
    status: int = 200
    paths: list[str] = field(default_factory=list[str])

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        assert scope["type"] == "http"
        self.paths.append(str(scope["path"]))
        while True:
            if not (await receive()).get("more_body", False):
                break
        await send(
            {
                "type": "http.response.start",
                "status": self.status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": self.body})


async def _serving(app: JwksHost) -> AsyncIterator[str]:
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(1_000):
            if server.started:
                break
            await asyncio.sleep(0.01)
        address: Any = server.servers[0].sockets[0].getsockname()
        yield f"http://127.0.0.1:{int(address[1])}"
    finally:
        server.should_exit = True
        await task


@pytest.fixture
async def host() -> AsyncIterator[tuple[JwksHost, str]]:
    app = JwksHost()
    async for url in _serving(app):
        yield app, url


@pytest.fixture
async def proxy() -> AsyncIterator[ProxyHandle]:
    async with running_proxy() as handle:
        yield handle


def _source(proxy: ProxyHandle) -> OutboundJwksKeySource:
    def client() -> SafeOutboundClient:
        return SafeOutboundClient(
            egress=EgressRoute(url=proxy.url, token=PROXY_TOKEN),
            connect_timeout=2.0,
            read_timeout=10.0,
        )

    return OutboundJwksKeySource(client)


def _cached_source(proxy: ProxyHandle, cache: JwksCache) -> OutboundJwksKeySource:
    def client() -> SafeOutboundClient:
        return SafeOutboundClient(
            egress=EgressRoute(url=proxy.url, token=PROXY_TOKEN),
            connect_timeout=2.0,
            read_timeout=10.0,
        )

    return OutboundJwksKeySource(client, cache)


async def test_a_second_resolve_reuses_the_cache_without_a_second_proxy_round_trip(
    host: tuple[JwksHost, str], proxy: ProxyHandle
) -> None:
    """Task-9 review finding E, proven at the level the module docstring
    promises: `test_jwks_key_source.py` already proves the caching logic
    against a fake transport; this is the same behaviour through the real
    `SafeOutboundClient`/`EgressProxy` pair, so a regression that only shows
    up once real HTTP is involved (a connection-pooling quirk, a header the
    fake never modeled) would still be caught here.
    """
    app, url = host
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm(RSAAlgorithm.SHA256).to_jwk(private_key.public_key()))
    jwk["kid"] = "acme-2026"
    app.body = json.dumps({"keys": [jwk]}).encode()
    token = jwt.encode({"sub": "x"}, private_key, algorithm="RS256", headers={"kid": "acme-2026"})
    cache = JwksCache()
    source = _cached_source(proxy, cache)
    jwks_url = f"{url}/.well-known/jwks.json"

    first = await source.resolve(public_key=None, jwks_url=jwks_url, token=token)
    second = await source.resolve(public_key=None, jwks_url=jwks_url, token=token)

    assert first is not None
    assert second == first
    # One fetch for two resolves — the real proxy only ever saw the first.
    assert app.paths == ["/.well-known/jwks.json"]


async def test_a_jwks_document_fetched_over_the_proxy_yields_a_usable_key(
    host: tuple[JwksHost, str], proxy: ProxyHandle
) -> None:
    app, url = host
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm(RSAAlgorithm.SHA256).to_jwk(private_key.public_key()))
    jwk["kid"] = "acme-2026"
    app.body = json.dumps({"keys": [jwk]}).encode()
    token = jwt.encode({"sub": "x"}, private_key, algorithm="RS256", headers={"kid": "acme-2026"})

    result = await _source(proxy).resolve(
        public_key=None, jwks_url=f"{url}/.well-known/jwks.json", token=token
    )

    assert result is not None
    assert jwt.decode(token, key=result, algorithms=["RS256"]) == {"sub": "x"}
    assert app.paths == ["/.well-known/jwks.json"]


async def test_a_404_from_the_jwks_endpoint_resolves_to_no_key(
    host: tuple[JwksHost, str], proxy: ProxyHandle
) -> None:
    app, url = host
    app.status = 404
    app.body = b"not found"

    result = await _source(proxy).resolve(
        public_key=None, jwks_url=f"{url}/missing.json", token="x.y.z"
    )

    assert result is None
