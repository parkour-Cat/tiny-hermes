"""The only way out of this process — and it leaves through the egress proxy.

Why this is not `httpx` with careful arguments: a library's redirect follower
re-sends on its own, without re-asking whether the new target is allowed and
without dropping the credential when the origin changes. That hop is precisely
the one an attacker controls — an endpoint that answers `302 Location:
http://169.254.169.254/` turns a vetted first request into an unvetted second
one. So the loop lives here, and **every hop is a fresh request to the proxy**,
which checks it from the start.

What moved out of this file, and why. Resolution, address pinning and the
address policy used to happen here; §16.5 puts them in the proxy. A library
binds only the code that imports it, and a sandbox imports nothing of ours.
Keeping a second copy here would not be a second check either: the socket this
client opens goes to the proxy, so an address vetted here is not an address
anything connects to. What stays is what only a client can do — follow a
redirect deliberately, drop a credential when the origin changes, cap a
response, and tell the proxy which layers to measure this request against.

Without an `EgressRoute` this client sends nothing. That is the stage's whole
point: there is no fallback branch, so "turn the proxy off and everything
stops" is a property of the code rather than a rule people remember.
"""

from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlencode, urljoin, urlsplit
from uuid import UUID

import httpx

from tiny_hermes.outbound.domain.address_policy import RefusalReason
from tiny_hermes.outbound.errors import (
    OutboundRefused,
    OutboundTooLarge,
    OutboundTooManyRedirects,
    OutboundUnreachable,
)

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
#: Statuses that keep the request as it was. The rest turn the next hop into a
#: `GET`, which is what every other client on the internet does and not
#: something this platform gets to reinterpret.
REPLAYING_STATUSES = frozenset({307, 308})

#: The header the proxy reads a caller's layers from. Named here rather than
#: imported from `egress`, so this module does not depend on the process it
#: talks to: they share a wire format, not a package.
SCOPE_HEADER = "X-Tiny-Hermes-Scope"

#: What the proxy marks its own refusals with. Without it, the boundary
#: answering 403 and the target answering 403 would look the same to a caller
#: — and one of those is worth retrying while the other never will be.
#:
#: Only a plaintext request can carry it back. A TLS target is reached through
#: `CONNECT`, and a refused tunnel has no response for a header to ride on, so
#: those refusals arrive as `egress_unavailable` and the specific rule is in the
#: proxy's log. Stated here rather than discovered later.
REFUSED_HEADER = "X-Tiny-Hermes-Egress-Refusal"

#: What the proxy marks a target it could not reach with. Different from a
#: refusal on purpose: nothing was sent, so nothing happened anywhere, and a
#: caller may reasonably try this one again.
UPSTREAM_HEADER = "X-Tiny-Hermes-Egress-Upstream"


@dataclass(frozen=True)
class EgressRoute:
    """Where this process's traffic leaves, and which layers it names.

    The ids go to the proxy, which looks each one up itself. They can only
    narrow what this request may reach — naming a layer is asking to be
    measured against it, never telling the proxy what to allow.
    """

    url: str
    token: str
    workspace_id: UUID | None = None
    agent_version_id: UUID | None = None
    run_id: UUID | None = None

    def headers(self) -> dict[str, str]:
        """What the proxy needs, and nothing a target should ever see.

        These ride on the `CONNECT` for a TLS target and on the forwarded
        request for a plaintext one. The proxy strips its own authorization
        before anything is passed on.
        """
        claimed = [
            f"{name}={value}"
            for name, value in (
                ("workspace", self.workspace_id),
                ("agent", self.agent_version_id),
                ("run", self.run_id),
            )
            if value is not None
        ]
        headers = {"Proxy-Authorization": f"Bearer {self.token}"}
        if claimed:
            headers[SCOPE_HEADER] = ";".join(claimed)
        return headers


def _reason(value: str) -> RefusalReason:
    """A refusal name off the wire, or a general one when this build is older.

    A proxy that has learned a reason this client has not is a proxy that is
    still refusing; falling back keeps the refusal a refusal rather than
    turning a deployment mismatch into a crash.
    """
    try:
        return RefusalReason(value.strip())
    except ValueError:
        return RefusalReason.EGRESS_UNAVAILABLE


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, (parts.hostname or ""), port


class SafeOutboundClient:
    """Makes requests to somewhere this process did not start.

    Built without a route, it refuses every call. A deployment that has not
    stood up a proxy therefore sends nothing, which is the same shape an empty
    `SANDBOX_IMAGE_DIGEST` gives the sandbox: unconfigured fails closed and
    says which setting is missing.
    """

    def __init__(
        self,
        *,
        egress: EgressRoute | None = None,
        connect_timeout: float = 5.0,
        read_timeout: float = 60.0,
        max_redirects: int = 5,
        max_response_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        self._egress = egress
        self._max_redirects = max_redirects
        self._max_response_bytes = max_response_bytes
        self._client = (
            None
            if egress is None
            else httpx.AsyncClient(  # noqa: TID251 - this is the one place
                follow_redirects=False,
                # Explicit platform policy, never an inherited process setting:
                # with `trust_env` on, whatever `HTTPS_PROXY` happened to be in
                # the environment would decide where this platform's traffic
                # goes.
                trust_env=False,
                proxy=httpx.Proxy(url=egress.url, headers=egress.headers()),
                timeout=httpx.Timeout(
                    connect=connect_timeout,
                    read=read_timeout,
                    write=read_timeout,
                    pool=connect_timeout,
                ),
            )
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
        if self._client is not None:
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
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        if json is not None and data is not None:
            # Two ways to say the same thing is a caller bug, not a request
            # this client gets to guess about.
            raise ValueError("request takes json or data, not both")
        sending: dict[str, str] = dict(headers) if headers else {}
        if data is not None:
            # RFC 6749 §4.1.3: a token endpoint takes a form body, not JSON —
            # this is the one caller (`identity/application/oidc_service.py`)
            # that needs it, so the encoding lives here rather than a second
            # client the OIDC flow builds for itself.
            content = urlencode(data).encode("ascii")
            sending.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif json is not None:
            # `httpx.Request(...).content` gives the encoded bytes and leaves
            # the header httpx would have sent with them behind. Declaring it
            # here is not cosmetic: a server is entitled to refuse a body it
            # was not told the type of, and one did — every model call went
            # out untyped and came back `415 Unsupported Media Type`, which
            # the Run reported as `endpoint_status:415`, pointing at the
            # endpoint rather than at this client.
            #
            # `setdefault`, so a caller that meant a vendor media type keeps
            # it.
            content = httpx.Request("POST", url, json=json).content
            sending.setdefault("Content-Type", "application/json")
        else:
            content = None
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
        if self._client is None:
            # No route, no call. There is deliberately no branch here that
            # connects directly: a fallback would turn "stop the proxy and
            # everything stops" into a sentence somebody has to keep true.
            raise OutboundRefused(RefusalReason.EGRESS_NOT_CONFIGURED)
        scheme, host, _ = _origin(url)
        if scheme not in ("http", "https"):
            raise OutboundRefused(RefusalReason.SCHEME_NOT_ALLOWED)
        if not host:
            raise OutboundRefused(RefusalReason.UNRESOLVED)
        # The name, unresolved. The proxy resolves it, checks the answer, and
        # connects to the literal it checked; a name pinned here would be
        # pinned for a connection that is not the one being made.
        request = self._client.build_request(method, url, content=content, headers=headers)
        return await self._read(request)

    async def _read(self, request: httpx.Request) -> httpx.Response:
        """Send, and stop reading rather than buffer an answer without end."""
        if self._client is None:  # pragma: no cover - `_send` refused already
            raise OutboundRefused(RefusalReason.EGRESS_NOT_CONFIGURED)
        try:
            response = await self._client.send(request, stream=True)
        except (httpx.ProxyError, httpx.ConnectError, httpx.ConnectTimeout) as failure:
            # The only socket this client opens goes to the proxy, so a
            # connection that failed is the boundary being unreachable — never
            # the endpoint, which this process has no way to reach directly.
            # Naming it as the endpoint would send an operator to the wrong
            # machine; the proxy reports an unreachable *target* separately,
            # with a header, and that path is right below.
            raise OutboundRefused(RefusalReason.EGRESS_UNAVAILABLE) from failure
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

        refused = response.headers.get(REFUSED_HEADER)
        if refused is not None:
            # The boundary said no, not the target. Turned back into a typed
            # refusal here so a Run's failure names the scope that stopped it,
            # and so nothing retries a decision that will not change.
            raise OutboundRefused(_reason(refused))
        if response.headers.get(UPSTREAM_HEADER) is not None:
            # The proxy tried and the target did not answer. Nothing was sent
            # through, so the effect is known to be none — the same thing a
            # failed connection used to mean when this client made its own.
            raise OutboundUnreachable(
                f"the target could not be reached: {request.url.host}",
                effect_unknown=False,
            )

        # `stream=True` leaves the response without content; giving it the bytes
        # that were actually read is what makes `.json()` and `.text` usable.
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            content=bytes(body),
            request=request,
        )
