"""What SafeOutboundClient does with a connection, proven against a real one.

The address policy itself is settled exhaustively in the unit tests. These tests
are about the plumbing built to obey it: pinning, redirects, header stripping,
and budgets. To reach a server on this machine at all they relax the policy in
one named way — loopback becomes reachable — because a stand-in endpoint has
nowhere else to live. The relaxation is explicit and local; `test_consults_the
_real_policy` proves the client still asks, so a relaxed test cannot hide a
client that skipped the question.
"""

from collections.abc import Sequence
from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit

import pytest
from tiny_hermes.outbound.client import SafeOutboundClient
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

from .conftest import StandIn

LOOPBACK = [ip_network("127.0.0.0/8")]


def loopback_is_reachable(
    addresses: Sequence[Address], approved: Sequence[Network]
) -> AddressVerdict:
    """The real policy, with loopback permitted so a local stand-in is usable."""
    if addresses and all(entry.is_loopback for entry in addresses):
        return AddressVerdict(allowed=True, address=addresses[0])
    return verdict(addresses, approved)


def build(**overrides: object) -> SafeOutboundClient:
    settings: dict[str, object] = {
        "approved": LOOPBACK,
        "policy": loopback_is_reachable,
        "connect_timeout": 2.0,
        "read_timeout": 5.0,
        "max_redirects": 5,
        "max_response_bytes": 1 << 20,
    }
    settings.update(overrides)
    return SafeOutboundClient(**settings)  # pyright: ignore[reportArgumentType]


async def test_an_ordinary_request_reaches_the_endpoint(
    stand_in: tuple[StandIn, str],
) -> None:
    app, url = stand_in
    async with build() as client:
        response = await client.post(f"{url}/ok", json={"say": "hello"})
    assert response.status_code == 200
    assert app.last().method == "POST"


async def test_an_ambient_proxy_cannot_replace_the_vetted_connection(
    stand_in: tuple[StandIn, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Address checks are meaningless if an inherited proxy makes the connection."""
    _, url = stand_in
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)

    async with build() as client:
        response = await client.post(f"{url}/ok", json={"say": "direct"})

    assert response.status_code == 200


async def test_consults_the_real_policy(stand_in: tuple[StandIn, str]) -> None:
    """With nothing relaxed, the stand-in is unreachable and never contacted.

    The second assertion is the one that matters: a client that connected first
    and checked afterwards would still raise, and would still be wrong.
    """
    app, url = stand_in
    async with build(policy=verdict) as client:
        with pytest.raises(OutboundRefused) as refusal:
            await client.post(f"{url}/ok", json={})
    assert refusal.value.reason is RefusalReason.LOOPBACK
    assert app.requests == []


async def test_plaintext_is_refused_outside_an_approved_range(
    stand_in: tuple[StandIn, str],
) -> None:
    """`http` is an operator's deliberate choice on their own network, not a default."""
    app, url = stand_in
    async with build(approved=[]) as client:
        with pytest.raises(OutboundRefused) as refusal:
            await client.post(f"{url}/ok", json={})
    assert refusal.value.reason is RefusalReason.PLAINTEXT_NOT_APPROVED
    assert app.requests == []


async def test_an_unsupported_scheme_is_refused() -> None:
    async with build() as client:
        with pytest.raises(OutboundRefused) as refusal:
            await client.post("ftp://example.com/ok", json={})
    assert refusal.value.reason is RefusalReason.SCHEME_NOT_ALLOWED


async def test_a_same_origin_redirect_is_followed(stand_in: tuple[StandIn, str]) -> None:
    app, url = stand_in
    async with build() as client:
        response = await client.post(f"{url}/redirect?to={url}/ok", json={})
    assert response.status_code == 200
    assert app.paths == ["/redirect", "/ok"]


async def test_a_redirect_to_a_forbidden_address_is_refused_at_that_hop(
    stand_in: tuple[StandIn, str],
) -> None:
    """The hop a library's own redirect follower takes without re-asking.

    The relaxation covers loopback only, so the second hop meets the real policy
    and the metadata address is refused there — which is the point: vetting the
    first address of a request says nothing about where it ends up.
    """
    app, url = stand_in
    async with build() as client:
        with pytest.raises(OutboundRefused) as refusal:
            await client.post(f"{url}/redirect?to=http://169.254.169.254/latest", json={})
    assert refusal.value.reason is RefusalReason.LINK_LOCAL
    assert app.paths == ["/redirect"]


async def test_a_temporary_redirect_replays_the_method_and_body(
    stand_in: tuple[StandIn, str],
) -> None:
    """307 preserves the request; 302 does not, and the difference is not ours to blur."""
    app, url = stand_in
    async with build() as client:
        await client.post(f"{url}/permanent?to={url}/ok", json={"say": "hello"})
    assert [entry.method for entry in app.requests] == ["POST", "POST"]


async def test_an_ordinary_redirect_becomes_a_get(stand_in: tuple[StandIn, str]) -> None:
    app, url = stand_in
    async with build() as client:
        await client.post(f"{url}/redirect?to={url}/ok", json={"say": "hello"})
    assert [entry.method for entry in app.requests] == ["POST", "GET"]


async def test_a_cross_origin_redirect_drops_the_credential(
    stand_in: tuple[StandIn, str], second_stand_in: tuple[StandIn, str]
) -> None:
    """Asserted on what the second server received, not on what the client meant."""
    first, first_url = stand_in
    second, second_url = second_stand_in
    async with build() as client:
        response = await client.post(
            f"{first_url}/redirect?to={second_url}/ok",
            json={},
            headers={"Authorization": "Bearer a-credential"},
        )
    assert response.status_code == 200
    assert "authorization" in first.last().headers
    assert "authorization" not in second.last().headers


async def test_a_same_origin_redirect_keeps_the_credential(
    stand_in: tuple[StandIn, str],
) -> None:
    app, url = stand_in
    async with build() as client:
        await client.post(
            f"{url}/redirect?to={url}/ok",
            json={},
            headers={"Authorization": "Bearer a-credential"},
        )
    assert app.last().path == "/ok"
    assert app.last().headers["authorization"] == "Bearer a-credential"


async def test_a_redirect_loop_ends(stand_in: tuple[StandIn, str]) -> None:
    app, url = stand_in
    async with build(max_redirects=3) as client:
        with pytest.raises(OutboundTooManyRedirects):
            await client.post(f"{url}/loop", json={})
    # The first request plus three hops, and then it stops rather than counting
    # the budget down somewhere off by one.
    assert len(app.requests) == 4


async def test_an_oversized_response_is_refused_rather_than_buffered(
    stand_in: tuple[StandIn, str],
) -> None:
    _, url = stand_in
    async with build(max_response_bytes=1024) as client:
        with pytest.raises(OutboundTooLarge):
            await client.post(f"{url}/huge", json={})


async def test_a_read_timeout_leaves_the_effect_unknown(
    stand_in: tuple[StandIn, str],
) -> None:
    """The request was sent. The endpoint may have done the work and billed for it."""
    app, url = stand_in
    # Long enough to outlast the read budget, short enough that the server is
    # not still sleeping when the fixture tries to shut it down.
    app.slow_seconds = 1.0
    async with build(read_timeout=0.2) as client:
        with pytest.raises(OutboundUnreachable) as failure:
            await client.post(f"{url}/slow", json={})
    assert failure.value.external_effect_unknown is True


async def test_a_connect_failure_leaves_no_doubt() -> None:
    """Nothing was sent, so nothing happened at the other end."""
    async with build(connect_timeout=0.5) as client:
        with pytest.raises(OutboundUnreachable) as failure:
            # Port 1 on this machine, which nothing listens on.
            await client.post("http://127.0.0.1:1/ok", json={})
    assert failure.value.external_effect_unknown is False


async def test_the_host_header_carries_the_name_not_the_pinned_address(
    stand_in: tuple[StandIn, str],
) -> None:
    """Pinning changes where the packet goes, never what the server is told.

    A stub resolver rather than `localhost`, which answers with both `::1` and
    `127.0.0.1` on some machines and would make this test depend on which one
    came first.
    """
    app, url = stand_in
    port = urlsplit(url).port

    def always_loopback(host: str, port: int) -> list[Address]:
        del host, port
        return [ip_address("127.0.0.1")]

    async with build(resolve=always_loopback) as client:
        await client.post(f"http://model.invalid:{port}/ok", json={})
    assert app.last().headers["host"] == f"model.invalid:{port}"


async def test_the_connection_goes_to_the_address_that_was_vetted(
    stand_in: tuple[StandIn, str],
) -> None:
    """The rebinding case, which no other test can see.

    A resolver that answers with a permitted address while it is being checked
    and a forbidden one by the time a socket opens is the whole reason the
    client connects to a literal. Real DNS cannot be made to lie on demand, so
    the resolver is a stub that changes its mind between calls.
    """
    app, url = stand_in
    port = urlsplit(url).port
    answers = [[ip_address("127.0.0.1")], [ip_address("169.254.169.254")]]

    def rebinding(host: str, port: int) -> list[Address]:
        del host, port
        return answers.pop(0) if answers else [ip_address("169.254.169.254")]

    async with build(resolve=rebinding) as client:
        response = await client.post(f"http://model.invalid:{port}/ok", json={})

    assert response.status_code == 200
    assert app.last().path == "/ok"
    # One resolution for this request, and the address it produced is the one
    # that was used. A second call would mean the socket asked again.
    assert len(answers) == 1
