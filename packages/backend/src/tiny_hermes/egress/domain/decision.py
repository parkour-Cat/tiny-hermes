"""Whether one connection may be opened, decided without opening anything.

Product design §16.5. The proxy exists so that this decision happens in a
process the caller does not control; this module is that decision, kept pure so
every class of refusal can be tested without a socket.

Three questions, in this order, and the order is the meaning:

1. **Is this caller anybody?** An unknown caller is refused before its target
   is even parsed, so an unauthenticated request never causes a DNS lookup.
2. **Was this target approved?** The intersection of the four layers, matched
   against the host or against the addresses it resolved to.
3. **Is this address one nobody may connect to?** `address_policy`'s question,
   unchanged from M1 — loopback, link-local, metadata, unapproved private space.

Cheapest first, and the expensive one last: resolution happens between 2 and 3
because a name nobody approved should not cost a lookup.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from tiny_hermes.outbound.domain.address_policy import (
    Address,
    AddressVerdict,
    Network,
    RefusalReason,
    verdict,
)
from tiny_hermes.outbound.domain.scope import OutboundScope, intersect

#: The two schemes a proxy serves. Anything else is a protocol this platform
#: has not decided how to bound, and an undecided protocol is a refusal.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Ports a target may listen on by default. Not a security boundary on its own
#: — a scope approves a host, not a service — but a scope entry cannot name a
#: port, so without this an approved host doubles as an approved SSH or database
#: server. A platform administrator may widen it, which is the same shape §16.5
#: gives every other widening: explicit, and only at the platform level.
ALLOWED_PORTS = frozenset({80, 443})


class CallerKind(StrEnum):
    """Who is asking, and how the proxy knows.

    `PLATFORM` proved it with a process token: the API, a Worker, the Scheduler.
    `SANDBOX` proved nothing and does not have to — its identity is the network
    it came from, which is a thing a process inside the container cannot forge
    by holding something.
    """

    PLATFORM = "platform"
    SANDBOX = "sandbox"


@dataclass(frozen=True)
class CallerClaim:
    """Which layers this request is asking to be measured against.

    The ids are a *claim* even from a trusted process: the proxy looks each one
    up itself and never accepts a scope the caller states inline. A trusted
    process could of course claim another workspace's id — it is trusted, and if
    it is compromised this is not the boundary that saves anything. The claim
    exists so the proxy can narrow, not so it can be told what to allow.
    """

    kind: CallerKind
    workspace_id: UUID | None = None
    agent_version_id: UUID | None = None
    run_id: UUID | None = None


@dataclass(frozen=True)
class ScopeLayers:
    """What each layer approved, as read for one request.

    A layer that is `None` is one this request does not have — a model call
    belongs to the platform and names no workspace. That is different from a
    layer that is present and empty, which approved nothing and therefore
    empties the chain.
    """

    platform: OutboundScope
    workspace: OutboundScope | None = None
    agent: OutboundScope | None = None
    run: OutboundScope | None = None

    def effective(self) -> OutboundScope:
        present = [
            layer
            for layer in (self.platform, self.workspace, self.agent, self.run)
            if layer is not None
        ]
        return intersect(*present)


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str
    port: int


class ProxyRefusal(StrEnum):
    """Why a connection was refused, in words the caller can act on.

    Separate from `address_policy.RefusalReason` because these are the proxy's
    own answers; an address refusal travels back under its own name so an
    operator reading a log can tell "nobody approved that" from "that address is
    one this platform never connects to".
    """

    UNKNOWN_CALLER = "unknown_caller"
    SCHEME_NOT_ALLOWED = "scheme_not_allowed"
    PORT_NOT_ALLOWED = "port_not_allowed"
    TARGET_NOT_IN_SCOPE = "target_not_in_scope"
    SCOPE_EMPTY = "scope_empty"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ProxyVerdict:
    """Allowed with the one address to connect to, or refused with a reason."""

    allowed: bool
    address: Address | None = None
    refusal: ProxyRefusal | None = None
    #: Set when the address policy is what refused, so the two vocabularies stay
    #: distinguishable in a log and in an audit row.
    address_reason: RefusalReason | None = None

    @property
    def reason_text(self) -> str:
        if self.allowed:
            return "allowed"
        if self.address_reason is not None:
            return self.address_reason.value
        return self.refusal.value if self.refusal is not None else "refused"


def check_request(
    target: Target,
    scope: OutboundScope,
    ports: frozenset[int] = ALLOWED_PORTS,
) -> ProxyRefusal | None:
    """Everything decidable before a name is resolved, or None to go on.

    Split from `decide` so the proxy can refuse an unapproved target without
    performing a lookup for it: a DNS query is a signal sent to somebody, and
    sending one on behalf of a request that was never going to be allowed is a
    small leak the boundary does not have to have.
    """
    if scope.empty:
        return ProxyRefusal.SCOPE_EMPTY
    if target.scheme not in ALLOWED_SCHEMES:
        return ProxyRefusal.SCHEME_NOT_ALLOWED
    if target.port not in ports:
        return ProxyRefusal.PORT_NOT_ALLOWED
    if not scope.allows_host(target.host):
        # A network entry can still approve this target, but only once the name
        # has resolved. `decide` answers that half.
        if not _has_network_entry(scope):
            return ProxyRefusal.TARGET_NOT_IN_SCOPE
    return None


#: The address policy as a seam, for the same reason `SafeOutboundClient` has
#: one: an integration test needs a server it can actually reach, and loopback
#: is refused by design. Relaxing it in a test is explicit and local, and one
#: test builds a proxy with the real policy to prove the question is still
#: asked.
AddressPolicy = Callable[[Sequence[Address], Sequence[Network]], AddressVerdict]


def decide(
    target: Target,
    scope: OutboundScope,
    addresses: Sequence[Address],
    approved: Sequence[Network] = (),
    policy: AddressPolicy = verdict,
    ports: frozenset[int] = ALLOWED_PORTS,
) -> ProxyVerdict:
    """The whole answer, given a resolution somebody else performed."""
    early = check_request(target, scope, ports)
    if early is not None:
        return ProxyVerdict(allowed=False, refusal=early)
    if not addresses:
        return ProxyVerdict(allowed=False, refusal=ProxyRefusal.UNRESOLVED)
    if not scope.allows_host(target.host) and not all(
        scope.allows_address(address) for address in addresses
    ):
        # Every address, not the first: a name that answers with one approved
        # and one unapproved address must be refused rather than raced, which is
        # the same rule `address_policy.verdict` follows for its own classes.
        return ProxyVerdict(allowed=False, refusal=ProxyRefusal.TARGET_NOT_IN_SCOPE)
    answer = policy(addresses, approved)
    if not answer.allowed:
        return ProxyVerdict(allowed=False, address_reason=answer.reason)
    return ProxyVerdict(allowed=True, address=answer.address)


def _has_network_entry(scope: OutboundScope) -> bool:
    return any(entry.network is not None for entry in scope.entries)
