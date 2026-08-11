"""The only way out of this process.

Why this is not `httpx` with careful arguments: a library's redirect follower
re-resolves and re-sends on its own, without re-asking whether the new target is
allowed and without dropping the credential when the origin changes. That hop is
precisely the one an attacker controls — an endpoint that answers `302
Location: http://169.254.169.254/` turns a vetted first request into an
unvetted second one. So the loop lives here, and every hop starts over from
resolution.

The other half is pinning. Vetting an address and then handing the hostname to a
socket leaves a window in which the name can resolve to something else, so the
connection is made to the literal address that was checked. The `Host` header
and the TLS SNI still carry the hostname, and certificate verification still
runs against it, so pinning changes where the packet goes and nothing about what
the far end is told or what this end will accept.
"""

import asyncio
from collections.abc import Callable, Sequence
from ipaddress import IPv6Address
from types import TracebackType
from typing import Any, Protocol, Self
from urllib.parse import urljoin, urlsplit

import httpx

from tiny_hermes.outbound.domain.address_policy import (
    Address,
    AddressVerdict,
    Network,
    RefusalReason,
    verdict,
)
from tiny_hermes.outbound.errors import (
    OutboundRefused,
    OutboundTooLarge,
    OutboundTooManyRedirects,
    OutboundUnreachable,
)
from tiny_hermes.outbound.resolver import lookup

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
#: Statuses that keep the request as it was. The rest turn the next hop into a
#: `GET`, which is what every other client on the internet does and not
#: something this platform gets to reinterpret.
REPLAYING_STATUSES = frozenset({307, 308})


class AddressPolicy(Protocol):
    def __call__(
        self, addresses: Sequence[Address], approved: Sequence[Network]
    ) -> AddressVerdict: ...


Resolver = Callable[[str, int], list[Address]]


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, (parts.hostname or ""), port


class SafeOutboundClient:
    """Makes requests to somewhere this process did not start.

    The policy and the resolver are collaborators so that a test can reach a
    stand-in server on this machine without loopback becoming approvable in
    production, and so that DNS rebinding — which real DNS will not perform on
    request — can be expressed at all.
    """

    def __init__(
        self,
        *,
        approved: Sequence[Network] = (),
        connect_timeout: float = 5.0,
        read_timeout: float = 60.0,
        max_redirects: int = 5,
        max_response_bytes: int = 10 * 1024 * 1024,
        policy: AddressPolicy = verdict,
        resolve: Resolver = lookup,
    ) -> None:
        self._approved = tuple(approved)
        self._max_redirects = max_redirects
        self._max_response_bytes = max_response_bytes
        self._policy = policy
        self._resolve = resolve
        self._client = httpx.AsyncClient(  # noqa: TID251 - this is the one place
            follow_redirects=False,
            # The client connects to the literal address it vetted. An ambient
            # HTTP proxy would replace that connection with one to the proxy
            # and make the address check and DNS pinning untrue. A future
            # enterprise proxy must therefore be explicit platform policy,
            # never an inherited process setting.
            trust_env=False,
            timeout=httpx.Timeout(
                connect=connect_timeout, read=read_timeout, write=read_timeout, pool=connect_timeout
            ),
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del kind, error, traceback
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def post(
        self,
        url: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return await self.request("POST", url, json=json, headers=headers)

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        sending: dict[str, str] = dict(headers) if headers else {}
        content = None if json is None else httpx.Request("POST", url, json=json).content
        for _ in range(self._max_redirects + 1):
            response = await self._send(method, url, content, sending)
            if response.status_code not in REDIRECT_STATUSES:
                return response
            location = response.headers.get("location", "")
            if not location:
                return response
            target = urljoin(url, location)
            if _origin(target) != _origin(url):
                # A credential is scoped to the origin it was issued for. The
                # header is dropped rather than the redirect refused, because an
                # endpoint moving to a CDN is ordinary and a leaked bearer token
                # is not.
                sending = {
                    name: value
                    for name, value in sending.items()
                    if name.lower() != "authorization"
                }
            if response.status_code not in REPLAYING_STATUSES:
                method, content = "GET", None
                sending = {
                    name: value
                    for name, value in sending.items()
                    if name.lower() != "content-type"
                }
            url = target
        raise OutboundTooManyRedirects(f"more than {self._max_redirects} redirects")

    async def _send(
        self, method: str, url: str, content: bytes | None, headers: dict[str, str]
    ) -> httpx.Response:
        scheme, host, port = _origin(url)
        if scheme not in ("http", "https"):
            raise OutboundRefused(RefusalReason.SCHEME_NOT_ALLOWED)
        if not host:
            raise OutboundRefused(RefusalReason.UNRESOLVED)

        addresses = await asyncio.to_thread(self._resolve, host, port)
        answer = self._policy(addresses, self._approved)
        if not answer.allowed or answer.address is None:
            raise OutboundRefused(answer.reason or RefusalReason.UNRESOLVED)
        pinned = answer.address
        if scheme == "http" and not self._is_approved(pinned):
            # Plaintext is an operator's deliberate choice on a network they
            # own, which is exactly what an approved range describes.
            raise OutboundRefused(RefusalReason.PLAINTEXT_NOT_APPROVED, address=str(pinned))

        literal = f"[{pinned}]" if isinstance(pinned, IPv6Address) else str(pinned)
        parts = urlsplit(url)
        pinned_url = parts._replace(netloc=f"{literal}:{port}").geturl()
        request = self._client.build_request(
            method,
            pinned_url,
            content=content,
            # The name, not the address: this is what the far end is told it is,
            # and what its certificate has to match.
            headers={**headers, "Host": parts.netloc},
            extensions={"sni_hostname": host},
        )
        return await self._read(request)

    async def _read(self, request: httpx.Request) -> httpx.Response:
        """Send, and stop reading rather than buffer an answer without end."""
        try:
            response = await self._client.send(request, stream=True)
        except httpx.ConnectError as failure:
            raise OutboundUnreachable(str(failure), effect_unknown=False) from failure
        except httpx.ConnectTimeout as failure:
            raise OutboundUnreachable(str(failure), effect_unknown=False) from failure
        except httpx.HTTPError as failure:
            # The request left this process. Whether the far end acted on it is
            # not knowable from here, and assuming it did not is how a Run gets
            # replayed against an endpoint that already charged for it.
            raise OutboundUnreachable(str(failure), effect_unknown=True) from failure

        body = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > self._max_response_bytes:
                    raise OutboundTooLarge(
                        f"response exceeded {self._max_response_bytes} bytes"
                    )
        except httpx.HTTPError as failure:
            raise OutboundUnreachable(str(failure), effect_unknown=True) from failure
        finally:
            await response.aclose()

        # `stream=True` leaves the response without content; giving it the bytes
        # that were actually read is what makes `.json()` and `.text` usable.
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            content=bytes(body),
            request=request,
        )

    def _is_approved(self, address: Address) -> bool:
        return any(
            address.version == network.version and address in network
            for network in self._approved
        )
