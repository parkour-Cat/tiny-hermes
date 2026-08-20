"""Which addresses this platform is allowed to open a connection to.

A pure function over addresses that have already been resolved, deliberately
knowing nothing about DNS, sockets, or HTTP. It is the security control the rest
of the outbound module is built to obey, and keeping it free of I/O is what lets
every class of refusal be tested exhaustively without a network.

Two properties matter more than the list itself:

- **Every** resolved address is checked, not the first one. A hostname under
  someone else's control can answer with a public address and a loopback address
  in the same response, and a connection would still be free to pick the second.
- Approval widens what is reachable *outside* this host and nothing else.
  Loopback, link-local, and the other unrouteable classes are refused ahead of
  the allowlist, so an operator who approves a range wide enough to contain them
  has not thereby opened the machine to itself.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_network

Address = IPv4Address | IPv6Address
Network = IPv4Network | IPv6Network

#: Python does not classify carrier-grade NAT as private, and the Alibaba Cloud
#: metadata address lives inside it, so the range is named here explicitly.
CARRIER_GRADE_NAT = ip_network("100.64.0.0/10")


class RefusalReason(StrEnum):
    """Why an address was refused, specific enough to act on.

    An operator reading an audit row needs to know which class they nearly
    leaked into: "private" and "the cloud metadata service" call for very
    different responses.
    """

    # Raised by the client rather than by `verdict`, but part of the same
    # vocabulary: an operator reading "this call was refused" should find one
    # list of reasons, not two.
    SCHEME_NOT_ALLOWED = "scheme_not_allowed"
    PLAINTEXT_NOT_APPROVED = "plaintext_not_approved"

    # Also raised by the client rather than by `verdict`, and the two are
    # different operational problems: nobody stood a proxy up, versus one is
    # standing and did not answer. A deployment with neither configured sends
    # nothing at all, which is the point.
    EGRESS_NOT_CONFIGURED = "egress_not_configured"
    EGRESS_UNAVAILABLE = "egress_unavailable"

    # The proxy's own answers, spelled the same here as there. A caller reads
    # them off a refused response and turns them back into a typed refusal, so
    # a Run's failure names the scope that stopped it rather than a status
    # code — and there is still one list of reasons, not two.
    UNKNOWN_CALLER = "unknown_caller"
    PORT_NOT_ALLOWED = "port_not_allowed"
    TARGET_NOT_IN_SCOPE = "target_not_in_scope"
    SCOPE_EMPTY = "scope_empty"

    UNRESOLVED = "unresolved"
    LOOPBACK = "loopback"
    LINK_LOCAL = "link_local"
    CARRIER_GRADE_NAT = "carrier_grade_nat"
    PRIVATE = "private"
    UNSPECIFIED = "unspecified"
    MULTICAST = "multicast"
    RESERVED = "reserved"


@dataclass(frozen=True)
class AddressVerdict:
    """Allowed, with the one address to connect to; or refused, with the reason.

    When allowed, ``address`` is always one of the addresses that was checked.
    The caller connects to that literal and to nothing else, which is what makes
    a record that changes between here and the socket unusable.
    """

    allowed: bool
    address: Address | None = None
    reason: RefusalReason | None = None


def _refusal(address: Address) -> RefusalReason | None:
    """The most specific class this address falls into, or None if it is fine.

    Order is the meaning. `ipaddress` reports several of these at once —
    `0.0.0.0` is unspecified and private, `240.0.0.1` is reserved and private —
    so the first match has to be the most specific one. Link-local precedes
    private so a cloud metadata address is never reported as merely private, and
    reserved precedes private so future-use space is not described as somebody's
    LAN.
    """
    if address.is_loopback:
        return RefusalReason.LOOPBACK
    if address.is_link_local:
        return RefusalReason.LINK_LOCAL
    if address.is_unspecified:
        return RefusalReason.UNSPECIFIED
    if address.is_multicast:
        return RefusalReason.MULTICAST
    if address.is_reserved:
        return RefusalReason.RESERVED
    if address.version == 4 and address in CARRIER_GRADE_NAT:
        return RefusalReason.CARRIER_GRADE_NAT
    if address.is_private:
        return RefusalReason.PRIVATE
    return None


def _approved(address: Address, approved: Sequence[Network]) -> bool:
    return any(address.version == network.version and address in network for network in approved)


def verdict(addresses: Sequence[Address], approved: Sequence[Network]) -> AddressVerdict:
    """Answer whether this name may be connected to, and at which address.

    A single forbidden address refuses the whole name. Anything less would let a
    resolver that returns both a permitted and a forbidden answer decide which
    one the connection lands on.
    """
    if not addresses:
        return AddressVerdict(allowed=False, reason=RefusalReason.UNRESOLVED)
    for address in addresses:
        reason = _refusal(address)
        if reason is None:
            continue
        # Only the routeable-but-restricted classes can be approved. The rest
        # never leave this host, so approving them is never what an operator
        # opening a private endpoint meant to do.
        opened = reason in (RefusalReason.PRIVATE, RefusalReason.CARRIER_GRADE_NAT)
        if opened and _approved(address, approved):
            continue
        return AddressVerdict(allowed=False, reason=reason)
    return AddressVerdict(allowed=True, address=addresses[0])
