"""What SafeOutboundClient does with a connection, proven against a real one.

Since M2C-1 the client cannot open a connection of its own: it goes through the
egress proxy, and these tests start a real one. What is left here is what only
a client can do — follow a redirect deliberately, drop a credential when the
origin changes, cap a response, and refuse when there is no route at all.

Where an address may be connected to is the proxy's question now, and the
suite next door settles it. The two tests here that still name an address are
the ones about the *client*: that a hop to a forbidden target is refused at
that hop rather than only at the first, and that a refusal from the boundary
comes back as a typed refusal rather than as somebody's 403.

Loopback is relaxed on the proxy the same way it always was on the client, and
`strict_proxy` keeps that honest.
"""

import pytest
from tiny_hermes.outbound.client import EgressRoute, SafeOutboundClient
from tiny_hermes.outbound.domain.address_policy import RefusalReason
from tiny_hermes.outbound.domain.scope import OutboundScope
from tiny_hermes.outbound.errors import (
    OutboundRefused,
    OutboundTooLarge,
    OutboundTooManyRedirects,
    OutboundUnreachable,
)

from ..egress_support import PROXY_TOKEN, ProxyHandle
from .conftest import StandIn


def build(proxy: ProxyHandle | None, **overrides: object) -> SafeOutboundClient:
    settings: dict[str, object] = {
        "egress": (
            None
            if proxy is None
            else EgressRoute(url=proxy.url, token=PROXY_TOKEN)
        ),
        "connect_timeout": 2.0,
        "read_timeout": 5.0,
        "max_redirects": 5,
        "max_response_bytes": 1 << 20,
    }
    settings.update(overrides)
    return SafeOutboundClient(**settings)  # pyright: ignore[reportArgumentType]


async def test_an_ordinary_request_reaches_the_endpoint(
    stand_in: tuple[StandIn, str], proxy: ProxyHandle
) -> None:
    app, url = stand_in
    async with build(proxy) as client:
        response = await client.post(f"{url}/ok", json={"say": "hello"})
    assert response.status_code == 200
    assert app.last().method == "POST"


async def test_an_ambient_proxy_cannot_replace_the_platform_s(
    stand_in: tuple[StandIn, str], proxy: ProxyHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Address checks are meaningless if an inherited proxy makes the connection."""
    _, url = stand_in
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)

    async with build(proxy) as client:
        response = await client.post(f"{url}/ok", json={"say": "direct"})

    assert response.status_code == 200


async def test_consults_the_real_policy(
    stand_in: tuple[StandIn, str], strict_proxy: ProxyHandle
) -> None:
    """With nothing relaxed, the stand-in is unreachable and never contacted.

    The second assertion is the one that matters: a client that connected first
    and checked afterwards would still raise, and would still be wrong.
    """
    app, url = stand_in
    async with build(strict_proxy) as client:
        with pytest.raises(OutboundRefused) as refusal:
            await client.post(f"{url}/ok", json={})
    assert refusal.value.reason is RefusalReason.LOOPBACK
    assert app.requests == []


async def test_plaintext_is_refused_outside_an_approved_range(
    stand_in: tuple[StandIn, str], plaintext_proxy: ProxyHandle
) -> None:
    """`http` is an operator's deliberate choice on their own network, not a
    default — and since M2C-1 the proxy is what enforces that, which is why the
    refusal still arrives here with its own name."""
    app, url = stand_in
    async with build(plaintext_proxy) as client:
        with pytest.raises(OutboundRefused) as refusal:
            await client.post(f"{url}/ok", json={})
    assert refusal.value.reason is RefusalReason.PLAINTEXT_NOT_APPROVED
    assert app.requests == []


async def test_an_unsupported_scheme_is_refused(proxy: ProxyHandle) -> None:
    async with build(proxy) as client:
        with pytest.raises(OutboundRefused) as refusal:
            await client.post("ftp://example.com/ok", json={})
    assert refusal.value.reason is RefusalReason.SCHEME_NOT_ALLOWED


async def test_a_same_origin_redirect_is_followed(
    stand_in: tuple[StandIn, str], proxy: ProxyHandle
) -> None:
    app, url = stand_in
    async with build(proxy) as client:
        response = await client.post(f"{url}/redirect?to={url}/ok", json={})
    assert response.status_code == 200
    assert app.paths == ["/redirect", "/ok"]


async def test_a_redirect_to_a_forbidden_address_is_refused_at_that_hop(
    stand_in: tuple[StandIn, str], proxy: ProxyHandle
) -> None:
    """The hop a library's own redirect follower takes without re-asking.

    The metadata address is put *into* the approved scope first, so this test
    is not merely watching an unapproved name be turned away: the second hop
    is one somebody explicitly allowed, and the address policy refuses it
    anyway. Vetting the first address of a request says nothing about where it
    ends up, and approving a name says nothing about what it resolves to.
    """
    app, url = stand_in
    proxy.directory.platform = OutboundScope.of(
        ["127.0.0.1", "localhost", "169.254.169.254"]
    )
    async with build(proxy) as client:
        with pytest.raises(OutboundRefused) as refusal:
            await client.post(f"{url}/redirect?to=http://169.254.169.254/latest", json={})
    assert refusal.value.reason is RefusalReason.LINK_LOCAL
    assert app.paths == ["/redirect"]


async def test_a_temporary_redirect_replays_the_method_and_body(
    stand_in: tuple[StandIn, str], proxy: ProxyHandle
) -> None:
    """307 preserves the request; 302 does not, and the difference is not ours to blur."""
    app, url = stand_in
    async with build(proxy) as client:
        await client.post(f"{url}/permanent?to={url}/ok", json={"say": "hello"})
    assert [entry.method for entry in app.requests] == ["POST", "POST"]


async def test_an_ordinary_redirect_becomes_a_get(
    stand_in: tuple[StandIn, str], proxy: ProxyHandle
) -> None:
    app, url = stand_in
    async with build(proxy) as client:
        await client.post(f"{url}/redirect?to={url}/ok", json={"say": "hello"})
    assert [entry.method for entry in app.requests] == ["POST", "GET"]


async def test_a_cross_origin_redirect_drops_the_credential(
    stand_in: tuple[StandIn, str],
    second_stand_in: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """Asserted on what the second server received, not on what the client meant."""
    first, first_url = stand_in
    second, second_url = second_stand_in
    async with build(proxy) as client:
        response = await client.post(
            f"{first_url}/redirect?to={second_url}/ok",
            json={},
            headers={"Authorization": "Bearer a-credential"},
        )
    assert response.status_code == 200
    assert "authorization" in first.last().headers
    assert "authorization" not in second.last().headers


async def test_a_same_origin_redirect_keeps_the_credential(
    stand_in: tuple[StandIn, str], proxy: ProxyHandle
) -> None:
    app, url = stand_in
    async with build(proxy) as client:
        await client.post(
            f"{url}/redirect?to={url}/ok",
            json={},
            headers={"Authorization": "Bearer a-credential"},
        )
    assert app.last().path == "/ok"
    assert app.last().headers["authorization"] == "Bearer a-credential"


async def test_a_redirect_loop_ends(
    stand_in: tuple[StandIn, str], proxy: ProxyHandle
) -> None:
    app, url = stand_in
    async with build(proxy, max_redirects=3) as client:
        with pytest.raises(OutboundTooManyRedirects):
            await client.post(f"{url}/loop", json={})
    # The first request plus three hops, and then it stops rather than counting
    # the budget down somewhere off by one.
    assert len(app.requests) == 4


async def test_an_oversized_response_is_refused_rather_than_buffered(
    stand_in: tuple[StandIn, str], proxy: ProxyHandle
) -> None:
    _, url = stand_in
    async with build(proxy, max_response_bytes=1024) as client:
        with pytest.raises(OutboundTooLarge):
            await client.post(f"{url}/huge", json={})


async def test_a_read_timeout_leaves_the_effect_unknown(
    stand_in: tuple[StandIn, str], proxy: ProxyHandle
) -> None:
    """The request was sent. The endpoint may have done the work and billed for it."""
    app, url = stand_in
    # Long enough to outlast the read budget, short enough that the server is
    # not still sleeping when the fixture tries to shut it down.
    app.slow_seconds = 1.0
    async with build(proxy, read_timeout=0.2) as client:
        with pytest.raises(OutboundUnreachable) as failure:
            await client.post(f"{url}/slow", json={})
    assert failure.value.external_effect_unknown is True


async def test_a_connect_failure_leaves_no_doubt(proxy: ProxyHandle) -> None:
    """Nothing was sent, so nothing happened at the other end."""
    async with build(proxy, connect_timeout=0.5) as client:
        with pytest.raises(OutboundUnreachable) as failure:
            # Port 1 on this machine, which nothing listens on.
            await client.post("http://127.0.0.1:1/ok", json={})
    assert failure.value.external_effect_unknown is False


async def test_the_client_does_not_resolve_anything_itself(
    stand_in: tuple[StandIn, str], proxy: ProxyHandle
) -> None:
    """The name travels; the proxy is what turns it into an address.

    Pinning and rebinding are settled in the egress suite, where they can be
    settled — this client opens one socket, to the proxy, so an address it
    vetted would not be an address anything connects to. Asserted from the
    outside: the stand-in sees the host it was asked for.
    """
    app, url = stand_in
    async with build(proxy) as client:
        await client.post(f"{url}/ok", json={})

    assert app.last().headers["host"] == url.removeprefix("http://")


# -- the boundary is not optional -------------------------------------------


async def test_without_a_route_nothing_is_sent_at_all(
    stand_in: tuple[StandIn, str],
) -> None:
    """The stage's exit check, from the caller's side.

    There is no branch in the client that connects directly, so a deployment
    that never configured a proxy sends nothing — and the refusal names the
    missing setting rather than looking like an unreachable endpoint.
    """
    app, url = stand_in
    async with build(None) as client:
        with pytest.raises(OutboundRefused) as refusal:
            await client.post(f"{url}/ok", json={})

    assert refusal.value.reason is RefusalReason.EGRESS_NOT_CONFIGURED
    assert app.requests == []


async def test_a_boundary_that_is_not_answering_is_named_as_itself(
    stand_in: tuple[StandIn, str],
) -> None:
    """An operator reading this should look at the proxy, not at the endpoint."""
    _, url = stand_in
    nowhere = EgressRoute(url="http://127.0.0.1:1", token=PROXY_TOKEN)
    async with SafeOutboundClient(egress=nowhere, connect_timeout=1.0) as client:
        with pytest.raises(OutboundRefused) as refusal:
            await client.post(f"{url}/ok", json={})

    assert refusal.value.reason is RefusalReason.EGRESS_UNAVAILABLE


async def test_a_refusal_from_the_boundary_is_not_the_target_saying_no(
    stand_in: tuple[StandIn, str], proxy: ProxyHandle
) -> None:
    """A 403 from the proxy and a 403 from the endpoint mean different things.

    One is a decision that will not change on a retry and the other might, so
    the client turns the first into a typed refusal naming the scope rather
    than handing back a response nobody can tell apart.
    """
    app, url = stand_in
    proxy.directory.platform = OutboundScope.of(["api.example.com"])
    async with build(proxy) as client:
        with pytest.raises(OutboundRefused) as refusal:
            await client.post(f"{url}/ok", json={})

    assert refusal.value.reason is RefusalReason.TARGET_NOT_IN_SCOPE
    assert app.requests == []


async def test_a_json_body_arrives_declared_as_json(
    stand_in: tuple[StandIn, str], proxy: ProxyHandle
) -> None:
    """A body sent as `json=` must carry `Content-Type: application/json`.

    It did not. The client encodes the body with
    `httpx.Request(...).content`, which returns the bytes and drops the
    header httpx would have sent with them, and only the `data=` branch set
    a content type of its own. Every JSON request this platform made went
    out with no content type at all.

    Servers are entitled to refuse that, and DeepSeek does: a real model
    endpoint answered `415 Unsupported Media Type` to every call, which the
    Run surfaced as `endpoint_status:415` — a message that points at the
    endpoint rather than at the client that malformed the request.
    """
    app, url = stand_in
    async with build(proxy) as client:
        response = await client.post(f"{url}/ok", json={"say": "hello"})

    assert response.status_code == 200
    assert app.last().headers.get("content-type") == "application/json"


async def test_a_caller_may_still_choose_its_own_content_type(
    stand_in: tuple[StandIn, str], proxy: ProxyHandle
) -> None:
    """The default must not overwrite a caller that meant something else —
    a JSON-shaped body with a vendor media type is somebody's real API."""
    app, url = stand_in
    async with build(proxy) as client:
        await client.post(
            f"{url}/ok",
            json={"say": "hello"},
            headers={"Content-Type": "application/vnd.example+json"},
        )

    assert app.last().headers.get("content-type") == "application/vnd.example+json"


async def test_a_compressed_answer_can_actually_be_read(
    stand_in: tuple[StandIn, str], proxy: ProxyHandle
) -> None:
    """`.json()` on a gzipped response, which every caller of this client does.

    `stream=True` plus `aiter_bytes()` hands back **decoded** bytes, and the
    rebuilt response carried the original `Content-Encoding: gzip` alongside
    them — so httpx decompressed a second time and raised `DecodingError:
    incorrect header check`. Every caller saw a transport failure for a
    request that had completed perfectly.

    Measured against a live tenant, and it cost more than an error: the Feishu
    reply dispatcher read that as "the send failed", retried five times, and
    sent the person five copies of the same message. The request had succeeded
    every time.
    """
    app, url = stand_in
    async with build(proxy) as client:
        response = await client.request("GET", f"{url}/gzipped")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "said": "compressed"}
    # The header is gone rather than kept, because the bytes it describes are
    # not compressed any more. Leaving it would be a claim about the body that
    # is false, and the next reader would meet the same failure.
    assert "content-encoding" not in response.headers
    assert app.last().path == "/gzipped"
