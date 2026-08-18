"""The boundary as a process: one connection in, one decision, then bytes.

Product design §16.5 requires the enforcement point to be outside the calling
process. `SafeOutboundClient` remains and still pins and still re-checks every
redirect — a library keeps a request well formed on its way out — but a library
binds only the code that imports it. This binds everything that has to leave
through a network it does not control.

What the proxy does per request:

1. Identify the caller. A process token proves a platform process; a sandbox
   proves nothing and is identified by the address it came from. An unknown
   caller is refused before its target is parsed, so it never costs a lookup.
2. Intersect the layers the claim names, refuse what is not approved, resolve,
   refuse again on the address policy, and **connect to the literal address that
   was checked**. Between a verdict and a socket there is no second resolution,
   so a name that changes its answer changes nothing.
3. Copy bytes, for a tunnel or for one forwarded request. Redirects are not
   followed here (see `domain/request.py`): the client's next hop arrives as a
   new request and is checked from the start.

Every refusal is answered in the protocol the caller is speaking — `403` with a
structured body, `502` when the target could not be reached — because a proxy
that answers every mistake with a bare `403 Forbidden` turns every misconfigured
scope into a packet capture.
"""

import asyncio
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from ipaddress import IPv6Address, ip_address
from typing import cast
from uuid import UUID

from tiny_hermes.egress.domain.decision import (
    ALLOWED_PORTS,
    AddressPolicy,
    CallerClaim,
    CallerKind,
    ProxyRefusal,
    ProxyVerdict,
    Target,
    decide,
)
from tiny_hermes.egress.domain.request import (
    MAX_HEAD_BYTES,
    ProxyRequest,
    ProxyRequestInvalid,
    parse_head,
)
from tiny_hermes.egress.ports.directory import ScopeDirectory
from tiny_hermes.outbound.domain.address_policy import Address, Network, verdict
from tiny_hermes.outbound.domain.scope import OutboundScope
from tiny_hermes.outbound.resolver import lookup

logger = logging.getLogger(__name__)

#: The header a trusted process states its layers in, as
#: `workspace=<uuid>;agent=<uuid>;run=<uuid>`. Any subset, and every value is
#: looked up rather than believed.
SCOPE_HEADER = "X-Tiny-Hermes-Scope"

#: How long a caller has to send a request head, and how long a target has to
#: accept a connection. Short: both are local decisions, and a proxy that waits
#: minutes on either is a proxy an idle client can exhaust.
HEAD_TIMEOUT_SECONDS = 10
CONNECT_TIMEOUT_SECONDS = 10

#: Bytes moved per copy step. Large enough that a download is not a syscall
#: storm, small enough that a stalled peer does not hold much.
CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class ProxySettings:
    """Everything the process needs that is not a lookup.

    ``token`` is what a platform process presents. It answers "is this one of
    ours" and nothing else — the scope still comes from the directory, so a
    leaked token widens nothing by itself. It is required: a proxy that accepts
    unauthenticated platform callers is a proxy any process on the network can
    borrow.
    """

    token: str
    #: Private ranges a platform administrator has opened, passed through to the
    #: address policy unchanged.
    approved_networks: Sequence[Network] = ()
    #: Ports an approved host may be reached on. Widening this is a platform
    #: administrator's decision and applies everywhere, which is why it lives
    #: with the process rather than with a workspace.
    allowed_ports: frozenset[int] = ALLOWED_PORTS
    host: str = "0.0.0.0"  # noqa: S104 - a proxy in a container must accept from its network
    port: int = 3128


class EgressProxy:
    """The server. One instance per process; one task per connection."""

    def __init__(
        self,
        directory: ScopeDirectory,
        settings: ProxySettings,
        resolve: Callable[[str, int], list[Address]] = lookup,
        policy: AddressPolicy = verdict,
    ) -> None:
        self._directory = directory
        self._settings = settings
        # Injected so a test can express DNS that changes its mind between the
        # check and the connection — the rebinding case the pinning exists for.
        self._resolve = resolve
        # The same seam `SafeOutboundClient` has, for the same reason: a test
        # server lives on loopback, which the real policy refuses.
        self._policy = policy

    async def serve(self, stop: asyncio.Event) -> None:
        server = await asyncio.start_server(
            self._handle, self._settings.host, self._settings.port
        )
        logger.info("egress proxy listening on %s:%s", self._settings.host, self._settings.port)
        async with server:
            await stop.wait()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await self._serve_one(reader, writer)
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            # A caller that connected and said nothing, or went away mid-head.
            # Not worth a log line: a health check does exactly this.
            pass
        except Exception:  # pragma: no cover - a connection must not kill the loop
            logger.exception("egress proxy connection failed")
        finally:
            writer.close()

    async def _serve_one(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        head = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), timeout=HEAD_TIMEOUT_SECONDS
        )
        if len(head) > MAX_HEAD_BYTES:
            await _answer(writer, 431, ProxyRefusal.UNKNOWN_CALLER, "request head too large")
            return
        try:
            request = parse_head(head)
        except ProxyRequestInvalid as invalid:
            await _answer(writer, 400, None, str(invalid))
            return

        claim = await self._identify(request, peer)
        if claim is None:
            # Before the target is looked at, so an unauthenticated caller
            # cannot use this proxy as a DNS oracle.
            await _answer(writer, 407, ProxyRefusal.UNKNOWN_CALLER, "unknown caller")
            return

        layers = await self._directory.layers_for(claim)
        target = Target(scheme=request.scheme, host=request.host, port=request.port)
        scope = layers.effective()
        addresses: list[Address] = []
        answer = self._decide(target, scope, addresses)
        if answer.refusal is not ProxyRefusal.UNRESOLVED:
            # Everything decidable without DNS was decided; only an otherwise
            # allowed target is worth resolving.
            if not answer.allowed:
                await self._refuse(writer, request, claim, answer)
                return
        addresses = await asyncio.to_thread(self._resolve, request.host, request.port)
        answer = self._decide(target, scope, addresses)
        if not answer.allowed or answer.address is None:
            await self._refuse(writer, request, claim, answer)
            return

        logger.info(
            "egress allowed: caller=%s host=%s port=%s run=%s",
            claim.kind.value,
            request.host,
            request.port,
            claim.run_id,
        )
        await self._connect(reader, writer, request, answer.address)

    def _decide(
        self, target: Target, scope: OutboundScope, addresses: list[Address]
    ) -> ProxyVerdict:
        return decide(
            target,
            scope,
            addresses,
            self._settings.approved_networks,
            self._policy,
            self._settings.allowed_ports,
        )

    async def _identify(self, request: ProxyRequest, peer: object) -> CallerClaim | None:
        authorization = request.header("Proxy-Authorization")
        if authorization is not None:
            scheme, _, value = authorization.partition(" ")
            if scheme.lower() != "bearer" or not _same(value.strip(), self._settings.token):
                return None
            return _claimed_layers(request.header(SCOPE_HEADER))
        # No token: the only caller allowed to have none is a sandbox, and it is
        # identified by where it came from rather than by what it presented.
        address = _peer_address(peer)
        if address is None:
            return None
        return await self._directory.sandbox_claim(address)

    async def _refuse(
        self,
        writer: asyncio.StreamWriter,
        request: ProxyRequest,
        claim: CallerClaim,
        verdict: ProxyVerdict,
    ) -> None:
        logger.info(
            "egress refused: caller=%s host=%s reason=%s run=%s",
            claim.kind.value,
            request.host,
            verdict.reason_text,
            claim.run_id,
        )
        await _answer(writer, 403, verdict.refusal, verdict.reason_text)

    async def _connect(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request: ProxyRequest,
        address: Address,
    ) -> None:
        """Open to the address that was checked, then move bytes.

        The literal address, never the name again: vetting a name and then
        handing it to a socket leaves a window in which it can resolve to
        something else.
        """
        literal = f"[{address}]" if isinstance(address, IPv6Address) else str(address)
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(literal, request.port),
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
        except (OSError, TimeoutError) as error:
            await _answer(writer, 502, None, f"upstream unreachable: {error}")
            return
        try:
            if request.tunnel:
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                upstream_writer.write(request.forwarded_head())
                await upstream_writer.drain()
            await asyncio.gather(
                _copy(reader, upstream_writer),
                _copy(upstream_reader, writer),
            )
        finally:
            upstream_writer.close()


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """One direction of a conversation, until it ends.

    Nothing is buffered whole: a proxy that held a body in memory would have a
    limit an upload could reach, and the limit would be a property of the
    boundary rather than of the transfer.
    """
    try:
        while True:
            chunk = await reader.read(CHUNK_BYTES)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, TimeoutError):
        pass
    finally:
        with_close = getattr(writer, "can_write_eof", None)
        if with_close is not None and writer.can_write_eof():
            try:
                writer.write_eof()
            except (OSError, ConnectionError):  # pragma: no cover - peer gone
                pass


async def _answer(
    writer: asyncio.StreamWriter,
    status: int,
    refusal: ProxyRefusal | None,
    detail: str,
) -> None:
    """A refusal in the protocol the caller is speaking, with its reason.

    JSON rather than prose so the client can turn it back into a typed refusal.
    `SafeOutboundClient` reads `reason` and raises `OutboundRefused` with it, so
    a Run's failure names the scope that stopped it instead of a status code.
    """
    body = json.dumps(
        {
            "error": "egress_refused" if status == 403 else "egress_failed",
            "reason": refusal.value if refusal is not None else None,
            "detail": detail,
        }
    ).encode()
    head = (
        f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("latin-1")
    writer.write(head + body)
    try:
        await writer.drain()
    except (ConnectionError, TimeoutError):  # pragma: no cover - caller gone
        pass


_REASONS = {
    400: "Bad Request",
    403: "Forbidden",
    407: "Proxy Authentication Required",
    431: "Request Header Fields Too Large",
    502: "Bad Gateway",
}


def _claimed_layers(header: str | None) -> CallerClaim:
    """The ids a trusted process states, parsed and not otherwise believed.

    A malformed value narrows to the platform layer rather than failing: the
    claim is a request to be measured against more layers, and losing it can
    only make the answer stricter.
    """
    claim = CallerClaim(kind=CallerKind.PLATFORM)
    if not header:
        return claim
    found: dict[str, UUID] = {}
    for part in header.split(";"):
        key, _, value = part.partition("=")
        try:
            found[key.strip().lower()] = UUID(value.strip())
        except ValueError:
            continue
    return CallerClaim(
        kind=CallerKind.PLATFORM,
        workspace_id=found.get("workspace"),
        agent_version_id=found.get("agent"),
        run_id=found.get("run"),
    )


def _peer_address(peer: object) -> Address | None:
    """The address a connection came from, which is a sandbox's whole identity.

    `peername` is `(host, port)` for IPv4 and a four-tuple for IPv6, and typed
    as `Any` by asyncio, so it is narrowed here rather than trusted.
    """
    if not isinstance(peer, tuple) or not peer:
        return None
    first = cast(tuple[object, ...], peer)[0]
    try:
        return ip_address(str(first))
    except ValueError:  # pragma: no cover - a socket always has an address
        return None


def _same(left: str, right: str) -> bool:
    """Constant-time comparison. A token check that leaks timing is a token
    check somebody can walk."""
    if len(left) != len(right):
        return False
    difference = 0
    for a, b in zip(left, right, strict=True):
        difference |= ord(a) ^ ord(b)
    return difference == 0
