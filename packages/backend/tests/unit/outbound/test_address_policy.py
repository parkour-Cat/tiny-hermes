from ipaddress import ip_address, ip_network

import pytest
from tiny_hermes.outbound.domain.address_policy import RefusalReason, verdict

# One row per class the platform must refuse, in both address families. These are
# not examples of a rule; they are the rule, and the reason each one is named is
# so a refusal can say which class it fell into rather than "not allowed".
REFUSED = [
    ("127.0.0.1", RefusalReason.LOOPBACK),
    ("127.10.20.30", RefusalReason.LOOPBACK),
    ("::1", RefusalReason.LOOPBACK),
    # The EC2 and GCP metadata address is link-local. Reported as link-local
    # rather than private, because an operator reading the audit row needs to
    # know which one they nearly leaked.
    ("169.254.169.254", RefusalReason.LINK_LOCAL),
    ("169.254.0.1", RefusalReason.LINK_LOCAL),
    ("fe80::1", RefusalReason.LINK_LOCAL),
    # The Alibaba Cloud metadata address. Python does not call carrier-grade NAT
    # private, so without an explicit range this one is reachable.
    ("100.100.100.200", RefusalReason.CARRIER_GRADE_NAT),
    ("100.64.0.1", RefusalReason.CARRIER_GRADE_NAT),
    ("100.127.255.254", RefusalReason.CARRIER_GRADE_NAT),
    ("10.0.0.5", RefusalReason.PRIVATE),
    ("172.16.0.1", RefusalReason.PRIVATE),
    ("172.31.255.254", RefusalReason.PRIVATE),
    ("192.168.1.1", RefusalReason.PRIVATE),
    ("fd00::1", RefusalReason.PRIVATE),
    ("0.0.0.0", RefusalReason.UNSPECIFIED),  # noqa: S104 - an address under test
    ("::", RefusalReason.UNSPECIFIED),
    ("224.0.0.1", RefusalReason.MULTICAST),
    ("ff02::1", RefusalReason.MULTICAST),
    ("240.0.0.1", RefusalReason.RESERVED),
]

ALLOWED = ["93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946"]


@pytest.mark.parametrize(("literal", "reason"), REFUSED)
def test_refuses_every_address_class(literal: str, reason: RefusalReason) -> None:
    answer = verdict([ip_address(literal)], approved=[])
    assert answer.allowed is False
    assert answer.reason is reason


@pytest.mark.parametrize("literal", ALLOWED)
def test_allows_ordinary_public_addresses(literal: str) -> None:
    answer = verdict([ip_address(literal)], approved=[])
    assert answer.allowed is True
    assert answer.address == ip_address(literal)


def test_an_approved_range_makes_a_private_address_reachable() -> None:
    """How an enterprise private endpoint becomes usable at all."""
    answer = verdict([ip_address("10.0.0.5")], approved=[ip_network("10.0.0.0/8")])
    assert answer.allowed is True
    assert answer.address == ip_address("10.0.0.5")


def test_approval_does_not_leak_past_its_own_range() -> None:
    answer = verdict([ip_address("10.1.0.5")], approved=[ip_network("10.0.0.0/24")])
    assert answer.allowed is False
    assert answer.reason is RefusalReason.PRIVATE


def test_approval_cannot_reach_loopback() -> None:
    """A range wide enough to cover loopback still does not.

    An operator approving `0.0.0.0/0` to reach one private endpoint must not
    thereby open the machine to itself. Loopback is refused ahead of the
    allowlist, so approval widens what is reachable outside this host and
    nothing else.
    """
    answer = verdict([ip_address("127.0.0.1")], approved=[ip_network("0.0.0.0/0")])
    assert answer.allowed is False
    assert answer.reason is RefusalReason.LOOPBACK


def test_one_forbidden_address_refuses_the_whole_name() -> None:
    """The bug this function exists to prevent.

    A hostname under an attacker's control can answer with a public address and
    a loopback address in the same response. Checking the first one, or any one,
    means the connection can still land on the second.
    """
    answer = verdict(
        [ip_address("93.184.216.34"), ip_address("127.0.0.1")], approved=[]
    )
    assert answer.allowed is False
    assert answer.reason is RefusalReason.LOOPBACK


def test_ordering_holds_whichever_way_the_resolver_answered() -> None:
    answer = verdict(
        [ip_address("127.0.0.1"), ip_address("93.184.216.34")], approved=[]
    )
    assert answer.allowed is False
    assert answer.reason is RefusalReason.LOOPBACK


def test_no_addresses_is_a_refusal_not_an_allowance() -> None:
    """An empty resolution must not fall through to "nothing forbidden here"."""
    answer = verdict([], approved=[])
    assert answer.allowed is False
    assert answer.reason is RefusalReason.UNRESOLVED


def test_the_chosen_address_is_one_of_the_ones_that_was_checked() -> None:
    """What the caller connects to, so it cannot be anything else."""
    addresses = [ip_address("93.184.216.34"), ip_address("8.8.8.8")]
    answer = verdict(addresses, approved=[])
    assert answer.allowed is True
    assert answer.address in addresses
