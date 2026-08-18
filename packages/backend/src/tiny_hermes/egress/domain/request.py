"""Reading a proxy request head, and nothing more of the conversation.

A forward proxy sees two shapes (RFC 9110 §7.1):

- `CONNECT host:443 HTTP/1.1` — the client wants a tunnel, and everything after
  the blank line is TLS the proxy cannot read. It checks the host, opens the
  connection to a pinned address, and copies bytes.
- `GET http://host/path HTTP/1.1` — absolute form, plain HTTP. The proxy
  forwards it with the target rewritten to origin form.

**This module never follows a redirect,** and that is a design decision rather
than an omission. A 3xx answer goes back to the client, which issues the next
request — and that request arrives here as a new one, checked from the start
against the same scope. So every hop is bounded without the proxy holding any
state, and stripping credentials across an origin change stays where the
credentials are: in the client. `outbound/client.py` does it, and its tests say
so.

Parsing stops at the head. Bodies are streamed through untouched: a proxy that
buffered a request body would be a proxy with a memory limit that an upload
could reach.
"""

from dataclasses import dataclass
from urllib.parse import urlsplit

#: Enough for a request line and headers, and small enough that a client
#: sending an unbounded head is refused rather than remembered.
MAX_HEAD_BYTES = 32 * 1024

DEFAULT_PORTS = {"http": 80, "https": 443}


class ProxyRequestInvalid(Exception):
    """A head this proxy will not act on, and why."""


@dataclass(frozen=True)
class ProxyRequest:
    """One request head, as the proxy needs it."""

    method: str
    scheme: str
    host: str
    port: int
    #: Origin-form target for a forwarded request; empty for `CONNECT`.
    path: str
    version: str
    headers: tuple[tuple[str, str], ...]

    @property
    def tunnel(self) -> bool:
        return self.method == "CONNECT"

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        for key, value in self.headers:
            if key.lower() == wanted:
                return value
        return None

    def forwarded_head(self) -> bytes:
        """The head to send on: origin form, minus the hop-by-hop headers.

        `Proxy-Authorization` is removed because it authenticates the caller to
        *this* proxy and is none of the target's business — sending it on would
        leak a platform credential to every host a Run talks to.
        """
        lines = [f"{self.method} {self.path} {self.version}"]
        for key, value in self.headers:
            if key.lower() in _HOP_BY_HOP:
                continue
            lines.append(f"{key}: {value}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


#: Headers that belong to this connection rather than to the message.
_HOP_BY_HOP = frozenset(
    {
        "proxy-authorization",
        "proxy-connection",
        "keep-alive",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def parse_head(head: bytes) -> ProxyRequest:
    """One request head, or a refusal naming what is wrong with it."""
    try:
        text = head.decode("latin-1")
    except UnicodeDecodeError as error:  # pragma: no cover - latin-1 takes all bytes
        raise ProxyRequestInvalid("the request head is not text") from error
    lines = text.split("\r\n")
    if not lines or not lines[0]:
        raise ProxyRequestInvalid("the request head is empty")
    method, _, rest = lines[0].partition(" ")
    target, _, version = rest.partition(" ")
    if not method or not target or not version.startswith("HTTP/"):
        raise ProxyRequestInvalid(f"{lines[0]!r} is not a request line")
    headers = _headers(lines[1:])
    if method.upper() == "CONNECT":
        host, port = _authority(target, default_port=DEFAULT_PORTS["https"])
        return ProxyRequest(
            method="CONNECT",
            scheme="https",
            host=host,
            port=port,
            path="",
            version=version,
            headers=headers,
        )
    split = urlsplit(target)
    if not split.scheme or not split.netloc:
        # Origin form reaches a proxy only from a client that thinks it is
        # talking to an ordinary server. Refused rather than guessed at: the
        # guess would be "whatever the Host header says", and a Host header is
        # the one part of this request nobody has checked.
        raise ProxyRequestInvalid(
            f"{target!r} is not absolute; a proxy request names its target in full"
        )
    host, port = _authority(split.netloc, default_port=DEFAULT_PORTS.get(split.scheme, 0))
    path = split.path or "/"
    if split.query:
        path = f"{path}?{split.query}"
    return ProxyRequest(
        method=method.upper(),
        scheme=split.scheme.lower(),
        host=host,
        port=port,
        path=path,
        version=version,
        headers=headers,
    )


def _headers(lines: list[str]) -> tuple[tuple[str, str], ...]:
    found: list[tuple[str, str]] = []
    for line in lines:
        if not line:
            break
        key, separator, value = line.partition(":")
        if not separator:
            raise ProxyRequestInvalid(f"{line!r} is not a header")
        found.append((key.strip(), value.strip()))
    return tuple(found)


def _authority(authority: str, *, default_port: int) -> tuple[str, int]:
    """Host and port out of an authority, refusing what cannot be one."""
    if "@" in authority:
        # Userinfo in a proxy target is a credential in a URL, and a credential
        # in a URL is one that ends up in a log.
        raise ProxyRequestInvalid("a proxy target may not carry userinfo")
    host = authority
    port = default_port
    if authority.startswith("["):
        closing = authority.find("]")
        if closing < 0:
            raise ProxyRequestInvalid(f"{authority!r} is not an authority")
        host = authority[1:closing]
        remainder = authority[closing + 1 :]
        if remainder.startswith(":"):
            port = _port(remainder[1:])
    elif ":" in authority:
        host, _, raw = authority.partition(":")
        port = _port(raw)
    host = host.strip().lower().rstrip(".")
    if not host:
        raise ProxyRequestInvalid("a proxy target needs a host")
    if port <= 0:
        raise ProxyRequestInvalid(f"{authority!r} names no port and no default applies")
    return host, port


def _port(raw: str) -> int:
    try:
        port = int(raw)
    except ValueError as error:
        raise ProxyRequestInvalid(f"{raw!r} is not a port") from error
    if not 1 <= port <= 65_535:
        raise ProxyRequestInvalid(f"{raw!r} is not a port")
    return port
