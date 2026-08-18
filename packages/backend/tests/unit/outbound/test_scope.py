"""Four layers of "may this go out", and the one operation that combines them.

Product design §16.5: the effective scope is 平台 ∩ 工作空间 ∩ Agent ∩ Run,
computed by the thing that opens the connection rather than trusted to whoever
filled in the admin form. Every test here is about that word — intersection —
because the whole security property is that no layer can widen the one above
it, and an implementation that "merged" scopes instead would pass a reading of
the sentence and fail its meaning.

This module is pure. Whether an address is one nobody should ever connect to is
`address_policy`'s question and stays there; this one only answers whether a
target was approved.
"""

from ipaddress import ip_address

import pytest
from tiny_hermes.outbound.domain.scope import (
    OutboundScope,
    ScopeEntryInvalid,
    intersect,
    parse_entry,
)


def scope(*entries: str) -> OutboundScope:
    return OutboundScope.of(entries)


# -- what one scope means ---------------------------------------------------


def test_an_exact_host_matches_itself_and_nothing_else() -> None:
    only = scope("api.example.com")

    assert only.allows_host("api.example.com") is True
    assert only.allows_host("other.example.com") is False
    assert only.allows_host("example.com") is False
    # Not a suffix match: `evil-api.example.com` ends with the approved name
    # and is a different host owned by somebody else.
    assert only.allows_host("evil-api.example.com") is False


def test_a_wildcard_matches_one_label_deep_and_not_the_bare_domain() -> None:
    """`*.example.com` is a statement a person can check at a glance.

    It covers the subdomains of `example.com` and not `example.com` itself,
    because approving a wildcard is approving what somebody else may create
    under a name, and the apex is a separate thing they already have.
    """
    any_sub = scope("*.example.com")

    assert any_sub.allows_host("api.example.com") is True
    assert any_sub.allows_host("deep.api.example.com") is True
    assert any_sub.allows_host("example.com") is False
    assert any_sub.allows_host("notexample.com") is False


def test_a_host_comparison_ignores_case_and_a_trailing_dot() -> None:
    """Both spellings reach the same server, so both are the same entry."""
    only = scope("api.example.com")

    assert only.allows_host("API.Example.COM") is True
    assert only.allows_host("api.example.com.") is True


def test_a_network_entry_matches_by_address_rather_than_by_name() -> None:
    inside = scope("10.1.0.0/16")

    assert inside.allows_address(ip_address("10.1.2.3")) is True
    assert inside.allows_address(ip_address("10.2.2.3")) is False
    # A name is not an address: an entry written as a network approves the
    # addresses in it, and says nothing about whatever resolves there.
    assert inside.allows_host("10.1.2.3") is False


def test_an_empty_scope_allows_nothing() -> None:
    """The platform default, and the answer to a layer that approved nothing."""
    nothing = OutboundScope.nothing()

    assert nothing.allows_host("api.example.com") is False
    assert nothing.allows_address(ip_address("93.184.216.34")) is False
    assert nothing.empty is True


@pytest.mark.parametrize(
    "entry",
    [
        "",
        "   ",
        "*",
        "*.com",
        "api-*.example.com",
        "*.*.example.com",
        "example.*",
        "http://api.example.com",
        "api.example.com/path",
        "api.example.com:443",
        "10.1.0.0/33",
    ],
)
def test_an_entry_nobody_could_check_at_a_glance_is_refused(entry: str) -> None:
    """A wildcard only in the leftmost label, and only below a real domain.

    `*` and `*.com` approve the internet through one line in a form. An
    interior wildcard is worse than useless: it is unreadable, so nobody
    reviewing the list can say what it opened.
    """
    with pytest.raises(ScopeEntryInvalid):
        parse_entry(entry)


def test_a_scheme_or_port_belongs_to_the_request_and_not_to_the_scope() -> None:
    """The refusal above says so; this one says what to write instead."""
    assert parse_entry("api.example.com").text == "api.example.com"
    assert parse_entry("*.example.com").text == "*.example.com"
    assert parse_entry("10.1.0.0/16").text == "10.1.0.0/16"


# -- the intersection -------------------------------------------------------


def test_the_intersection_of_nothing_is_empty_rather_than_everything() -> None:
    """A vacuous "all" here would be the whole bug this module exists to avoid."""
    assert intersect().empty is True


def test_a_narrower_layer_wins_over_a_wider_one() -> None:
    effective = intersect(scope("*.example.com"), scope("api.example.com"))

    assert effective.allows_host("api.example.com") is True
    assert effective.allows_host("other.example.com") is False


def test_the_order_of_the_layers_does_not_change_the_result() -> None:
    wide = scope("*.example.com")
    narrow = scope("api.example.com")

    assert intersect(wide, narrow).entries == intersect(narrow, wide).entries


def test_a_layer_cannot_open_a_target_the_layer_above_it_did_not() -> None:
    """The rule, stated as the test it is: workspace approved, Agent did not.

    And the direction that matters more — an Agent naming something the
    workspace never approved gets nothing, not its own entry.
    """
    workspace = scope("api.example.com", "docs.example.com")
    agent = scope("api.example.com")
    assert intersect(workspace, agent).allows_host("docs.example.com") is False

    rogue = scope("payments.example.com")
    assert intersect(workspace, rogue).empty is True


def test_an_empty_layer_anywhere_empties_the_whole_chain() -> None:
    platform = scope("*.example.com")
    workspace = OutboundScope.nothing()
    agent = scope("api.example.com")

    assert intersect(platform, workspace, agent).empty is True


def test_two_networks_intersect_to_the_smaller_one() -> None:
    effective = intersect(scope("10.0.0.0/8"), scope("10.1.0.0/16"))

    assert effective.allows_address(ip_address("10.1.2.3")) is True
    assert effective.allows_address(ip_address("10.2.2.3")) is False


def test_disjoint_networks_intersect_to_nothing() -> None:
    assert intersect(scope("10.1.0.0/16"), scope("10.2.0.0/16")).empty is True


def test_a_host_and_a_network_do_not_intersect_each_other() -> None:
    """They are different kinds of claim. A name is not resolved here — this
    module has no DNS — so `api.example.com` and `10.1.0.0/16` share nothing
    that can be decided without one."""
    assert intersect(scope("api.example.com"), scope("10.1.0.0/16")).empty is True


def test_four_layers_narrow_step_by_step() -> None:
    """The shape §16.5 actually names, including the fourth layer."""
    effective = intersect(
        scope("*.example.com", "*.other.example"),
        scope("*.example.com"),
        scope("api.example.com", "docs.example.com"),
        scope("api.example.com"),
    )

    assert effective.entries == (parse_entry("api.example.com"),)
    assert effective.allows_host("api.example.com") is True
    assert effective.allows_host("docs.example.com") is False
    assert effective.allows_host("anything.other.example") is False


def test_the_result_is_stable_and_deduplicated() -> None:
    """Two reads of the same configuration produce the same list, in order."""
    effective = intersect(
        scope("*.example.com", "api.example.com"), scope("api.example.com")
    )

    assert effective.entries == (parse_entry("api.example.com"),)
