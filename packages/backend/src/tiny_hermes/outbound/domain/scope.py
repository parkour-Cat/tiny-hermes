"""What a layer approved, and what four layers approve together.

Product design §16.5: 有效出站范围是「平台允许范围 ∩ 工作空间允许范围 ∩ Agent 白名单
∩ 本次 Run 或委派范围」, computed where the connection is made. This module is
that sentence and nothing else — pure, no DNS, no sockets, no database.

Two things it deliberately does not do.

**It does not resolve names.** A scope entry is either a host pattern or a
network, and the two never intersect each other here: deciding that
`api.example.com` is inside `10.1.0.0/16` needs a resolver, and a resolver's
answer changes between the moment a scope is computed and the moment a packet
leaves. The connection path resolves once and checks both, in that order.

**It does not replace `address_policy`.** Approval says a target was *chosen*;
the address policy says an address is one nobody should ever connect to. An
operator who approves `10.0.0.0/8` has not thereby approved this host's own
loopback, and that separation lives there rather than here.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_network

Address = IPv4Address | IPv6Address
Network = IPv4Network | IPv6Network

#: A DNS label, as a scope is allowed to write one. No underscores and no
#: leading or trailing hyphen: an entry that cannot be a real name is an entry
#: nobody can act on.
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

#: The shortest thing a wildcard may stand under. `*.com` and `*` approve the
#: internet through one line in a form, so a wildcard needs a domain beneath it
#: that somebody actually controls.
MINIMUM_WILDCARD_LABELS = 2


class ScopeEntryInvalid(Exception):
    """An entry this platform will not store, and why.

    Carries prose rather than a code: every rejection here is something the
    person typing it can fix by typing something else.
    """


@dataclass(frozen=True)
class ScopeEntry:
    """One approved target: a host, a one-level wildcard, or a network."""

    text: str
    network: Network | None = None
    wildcard: bool = False

    def matches_host(self, host: str) -> bool:
        if self.network is not None:
            return False
        candidate = _clean_host(host)
        if not self.wildcard:
            return candidate == self.text
        # `*.example.com` covers what is under `example.com` and not the apex:
        # approving a wildcard approves what somebody may create beneath a name
        # they already have, which is a different statement from approving the
        # name itself.
        suffix = self.text[1:]
        return candidate.endswith(suffix) and candidate != suffix[1:]

    def matches_address(self, address: Address) -> bool:
        if self.network is None:
            return False
        return address.version == self.network.version and address in self.network

    def contains(self, other: "ScopeEntry") -> bool:
        """Whether this entry approves everything `other` does.

        The whole of the intersection: a narrower entry survives when a wider
        one covers it, and neither survives when they merely overlap in a way
        no single entry can express.
        """
        if self.network is not None or other.network is not None:
            if self.network is None or other.network is None:
                return False
            return self.network.version == other.network.version and other.network.subnet_of(
                self.network  # type: ignore[arg-type] - versions checked above
            )
        if self.wildcard:
            return self.text == other.text or self.matches_host(other.text.lstrip("*."))
        return self.text == other.text and not other.wildcard


def parse_entry(text: str) -> ScopeEntry:
    """One entry, or a refusal naming what is wrong with it."""
    cleaned = text.strip().lower()
    if not cleaned:
        raise ScopeEntryInvalid("an outbound entry cannot be blank")
    if "/" in cleaned and not cleaned.startswith("*"):
        try:
            network = ip_network(cleaned, strict=False)
        except ValueError as error:
            raise ScopeEntryInvalid(f"{text!r} is not a network: {error}") from error
        # Normalized through `ip_network`, so `10.1.0.5/16` and `10.1.0.0/16`
        # are one entry rather than two spellings that look different in a list.
        return ScopeEntry(text=str(network), network=network)
    if "://" in cleaned or "/" in cleaned:
        raise ScopeEntryInvalid(
            f"{text!r} looks like a URL; an outbound entry is a host or a network"
        )
    if ":" in cleaned:
        raise ScopeEntryInvalid(
            f"{text!r} names a port; a scope approves a target, and the port "
            "belongs to the request"
        )
    host = _clean_host(cleaned)
    wildcard = host.startswith("*.")
    labels = host.split(".")
    if wildcard:
        labels = labels[1:]
        if len(labels) < MINIMUM_WILDCARD_LABELS:
            raise ScopeEntryInvalid(
                f"{text!r} is too wide; a wildcard needs a domain under it, "
                "such as *.example.com"
            )
    if not labels or any(not _LABEL.match(label) for label in labels):
        raise ScopeEntryInvalid(
            f"{text!r} is not a host; a wildcard may only replace the leftmost label"
        )
    return ScopeEntry(text=host, wildcard=wildcard)


@dataclass(frozen=True)
class OutboundScope:
    """One layer's answer to "what may be reached".

    Empty means nothing, never everything. That is the platform default and the
    reading of a layer that approved nothing — a deployment that has configured
    no outbound scope sends nothing, the same way one that has approved no
    sandbox image runs no tool.
    """

    entries: tuple[ScopeEntry, ...] = ()

    @classmethod
    def nothing(cls) -> "OutboundScope":
        """The empty scope, named as a constructor so `empty` can stay a fact.

        Read at call sites as "this layer approves nothing", which is what the
        platform default is and what a chain containing it comes to.
        """
        return cls(entries=())

    @classmethod
    def of(cls, entries: Iterable[str]) -> "OutboundScope":
        parsed = [parse_entry(text) for text in entries]
        return cls(entries=_deduplicated(parsed))

    #: Read as a word rather than as `not entries` at call sites, because "this
    #: layer approved nothing" is the sentence the security rule is written in.
    @property
    def empty(self) -> bool:
        return not self.entries

    def allows_host(self, host: str) -> bool:
        return any(entry.matches_host(host) for entry in self.entries)

    def allows_address(self, address: Address) -> bool:
        return any(entry.matches_address(address) for entry in self.entries)


def intersect(*scopes: OutboundScope) -> OutboundScope:
    """The effective scope of a chain of layers.

    No arguments is empty, not everything. A vacuous "all" would turn a missing
    layer into an open door, and a missing layer is exactly what a bug looks
    like here.

    An entry survives when *every* layer covers it, and what survives is the
    narrower of the two whenever one contains the other. Two entries that merely
    overlap — `*.a.example` and `*.b.example` — produce nothing, because no
    single entry expresses their overlap and inventing one would be inventing an
    approval nobody gave.
    """
    if not scopes:
        return OutboundScope.nothing()
    surviving = list(scopes[0].entries)
    for layer in scopes[1:]:
        surviving = _narrow(surviving, layer.entries)
        if not surviving:
            return OutboundScope.nothing()
    return OutboundScope(entries=_deduplicated(surviving))


def _narrow(
    current: Sequence[ScopeEntry], limit: Sequence[ScopeEntry]
) -> list[ScopeEntry]:
    kept: list[ScopeEntry] = []
    for entry in current:
        for allowed in limit:
            if allowed.contains(entry):
                kept.append(entry)
                break
            if entry.contains(allowed):
                kept.append(allowed)
                break
    return kept


def _deduplicated(entries: Sequence[ScopeEntry]) -> tuple[ScopeEntry, ...]:
    """Stable order, one of each. Two reads of one configuration agree."""
    seen: dict[str, ScopeEntry] = {}
    for entry in entries:
        seen.setdefault(entry.text, entry)
    return tuple(seen.values())


def _clean_host(host: str) -> str:
    """One spelling of a name. `API.Example.COM.` and `api.example.com` reach
    the same server, so they are the same entry."""
    return host.strip().lower().rstrip(".")
