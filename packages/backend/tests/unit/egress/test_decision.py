"""What the proxy decides, without a socket anywhere near it.

Three questions in a fixed order (§16.5): who is asking, was this target
approved, and is this address one nobody may connect to. The order is a
property worth testing on its own — an unapproved target must not cost a DNS
lookup, and an approved one must still meet the address policy.
"""

from ipaddress import ip_address, ip_network

import pytest
from tiny_hermes.egress.domain.decision import (
    ALLOWED_PORTS,
    CallerClaim,
    CallerKind,
    ProxyRefusal,
    ScopeLayers,
    Target,
    check_request,
    decide,
)
from tiny_hermes.outbound.domain.address_policy import RefusalReason
from tiny_hermes.outbound.domain.scope import OutboundScope

PUBLIC = ip_address("93.184.216.34")
METADATA = ip_address("169.254.169.254")
PRIVATE = ip_address("10.1.2.3")


def scope(*entries: str) -> OutboundScope:
    return OutboundScope.of(entries)


def target(host: str = "api.example.com", port: int = 443, scheme: str = "https") -> Target:
    return Target(scheme=scheme, host=host, port=port)


# -- the layers -------------------------------------------------------------


def test_a_layer_the_claim_did_not_name_does_not_narrow_anything() -> None:
    """A model call belongs to the platform and names no workspace."""
    layers = ScopeLayers(platform=scope("*.example.com"))

    assert layers.effective().allows_host("api.example.com") is True


def test_a_layer_that_is_present_and_empty_closes_the_chain() -> None:
    """Different from absent, and the difference is the whole security rule."""
    layers = ScopeLayers(
        platform=scope("*.example.com"), workspace=OutboundScope.nothing()
    )

    assert layers.effective().empty is True


def test_every_named_layer_narrows_in_turn() -> None:
    layers = ScopeLayers(
        platform=scope("*.example.com"),
        workspace=scope("api.example.com", "docs.example.com"),
        agent=scope("api.example.com"),
        run=scope("api.example.com"),
    )

    effective = layers.effective()
    assert effective.allows_host("api.example.com") is True
    assert effective.allows_host("docs.example.com") is False


# -- what is decided before DNS ---------------------------------------------


def test_an_unapproved_host_is_refused_without_needing_a_lookup() -> None:
    """The reason this split exists: a DNS query is a signal sent to somebody,
    and a request that was never going to be allowed should not send one."""
    early = check_request(target("other.example.com"), scope("api.example.com"))

    assert early is ProxyRefusal.TARGET_NOT_IN_SCOPE


def test_an_empty_scope_is_refused_as_itself_rather_than_as_a_missing_target() -> None:
    """An operator reading "scope_empty" knows to go and approve something;
    one reading "target_not_in_scope" goes looking for a typo."""
    assert check_request(target(), OutboundScope.nothing()) is ProxyRefusal.SCOPE_EMPTY


@pytest.mark.parametrize("scheme", ["ftp", "gopher", "file", "ws"])
def test_a_protocol_this_platform_has_not_bounded_is_refused(scheme: str) -> None:
    early = check_request(target(scheme=scheme), scope("api.example.com"))

    assert early is ProxyRefusal.SCHEME_NOT_ALLOWED


@pytest.mark.parametrize("port", [22, 25, 3306, 5432, 8080])
def test_an_approved_host_is_not_thereby_an_approved_ssh_server(port: int) -> None:
    """A scope entry cannot name a port, so without this an approved host is an
    approved everything-it-listens-on."""
    assert port not in ALLOWED_PORTS
    early = check_request(target(port=port), scope("api.example.com"))

    assert early is ProxyRefusal.PORT_NOT_ALLOWED


def test_a_scope_holding_a_network_waits_for_the_lookup() -> None:
    """A network entry can only be matched against an address, so the name
    alone cannot refuse it."""
    assert check_request(target(), scope("10.1.0.0/16")) is None


# -- the whole answer -------------------------------------------------------


def test_an_approved_host_that_resolves_publicly_is_allowed_at_that_address() -> None:
    answer = decide(target(), scope("api.example.com"), [PUBLIC])

    assert answer.allowed is True
    assert answer.address == PUBLIC


def test_a_name_that_resolves_to_nothing_is_refused_as_unresolved() -> None:
    answer = decide(target(), scope("api.example.com"), [])

    assert answer.allowed is False
    assert answer.refusal is ProxyRefusal.UNRESOLVED


def test_an_approved_name_pointing_at_the_metadata_service_is_still_refused() -> None:
    """Approval says a target was chosen; the address policy says which
    addresses nobody may connect to. Approving a name does not buy past it."""
    answer = decide(target(), scope("api.example.com"), [METADATA])

    assert answer.allowed is False
    assert answer.address_reason is RefusalReason.LINK_LOCAL
    assert answer.reason_text == "link_local"


def test_a_name_answering_with_one_good_and_one_forbidden_address_is_refused() -> None:
    """Anything less would let the resolver decide which one the socket lands
    on."""
    answer = decide(target(), scope("api.example.com"), [PUBLIC, METADATA])

    assert answer.allowed is False


def test_a_private_address_needs_both_the_scope_and_the_approved_range() -> None:
    inside = scope("10.1.0.0/16")

    assert decide(target(), inside, [PRIVATE]).allowed is False
    approved = decide(target(), inside, [PRIVATE], [ip_network("10.1.0.0/16")])
    assert approved.allowed is True
    assert approved.address == PRIVATE


def test_a_network_scope_refuses_a_name_that_resolves_outside_it() -> None:
    answer = decide(target(), scope("10.1.0.0/16"), [PUBLIC])

    assert answer.allowed is False
    assert answer.refusal is ProxyRefusal.TARGET_NOT_IN_SCOPE


def test_a_network_scope_refuses_a_name_only_partly_inside_it() -> None:
    """Every address, again: half an answer inside an approved range is not an
    approved name."""
    answer = decide(
        target(),
        scope("10.1.0.0/16"),
        [PRIVATE, ip_address("10.9.9.9")],
        [ip_network("10.0.0.0/8")],
    )

    assert answer.allowed is False


def test_a_claim_carries_who_is_asking_and_which_layers_to_use() -> None:
    """The sandbox kind exists so a reader can tell the two identities apart in
    a log: one proved a token, the other proved where it came from."""
    claim = CallerClaim(kind=CallerKind.SANDBOX, run_id=None)

    assert claim.kind.value == "sandbox"
    assert claim.workspace_id is None
