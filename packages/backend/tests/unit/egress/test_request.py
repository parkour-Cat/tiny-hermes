"""Reading a proxy request head, and refusing the ones nobody could act on.

A forward proxy sees two shapes and this module answers both. The tests worth
reading twice are the refusals: userinfo in a target (a credential that ends up
in a log), origin form (a target only the unchecked `Host` header could supply),
and the hop-by-hop headers that must not travel on — `Proxy-Authorization`
authenticates the caller to this proxy and is none of the target's business.
"""

import pytest
from tiny_hermes.egress.domain.request import (
    ProxyRequestInvalid,
    parse_head,
)


def head(line: str, *headers: str) -> bytes:
    return ("\r\n".join([line, *headers]) + "\r\n\r\n").encode()


def test_a_tunnel_names_its_authority_and_carries_no_path() -> None:
    request = parse_head(head("CONNECT api.example.com:443 HTTP/1.1", "Host: api.example.com"))

    assert request.tunnel is True
    assert (request.scheme, request.host, request.port) == ("https", "api.example.com", 443)
    assert request.path == ""


def test_a_tunnel_without_a_port_is_read_as_https() -> None:
    request = parse_head(head("CONNECT api.example.com HTTP/1.1"))

    assert request.port == 443


def test_an_absolute_request_keeps_its_path_and_query() -> None:
    request = parse_head(head("GET http://api.example.com/v1/thing?a=1 HTTP/1.1"))

    assert (request.method, request.scheme, request.host, request.port) == (
        "GET",
        "http",
        "api.example.com",
        80,
    )
    assert request.path == "/v1/thing?a=1"


def test_an_absolute_request_with_no_path_is_forwarded_as_the_root() -> None:
    assert parse_head(head("GET http://api.example.com HTTP/1.1")).path == "/"


def test_a_host_is_compared_in_one_spelling() -> None:
    request = parse_head(head("CONNECT API.Example.COM.:443 HTTP/1.1"))

    assert request.host == "api.example.com"


def test_an_ipv6_authority_keeps_its_address_without_the_brackets() -> None:
    request = parse_head(head("CONNECT [2606:2800:220:1:248:1893:25c8:1946]:443 HTTP/1.1"))

    assert request.host == "2606:2800:220:1:248:1893:25c8:1946"
    assert request.port == 443


def test_origin_form_is_refused_rather_than_guessed_at() -> None:
    """The guess would be "whatever the Host header says", and that header is
    the one part of this request nobody has checked."""
    with pytest.raises(ProxyRequestInvalid):
        parse_head(head("GET /v1/thing HTTP/1.1", "Host: api.example.com"))


def test_a_credential_in_the_target_is_refused() -> None:
    with pytest.raises(ProxyRequestInvalid):
        parse_head(head("CONNECT user:secret@api.example.com:443 HTTP/1.1"))


@pytest.mark.parametrize(
    "line",
    [
        "",
        "CONNECT",
        "CONNECT api.example.com:443",
        "CONNECT api.example.com:443 SPDY/3",
        "CONNECT :443 HTTP/1.1",
        "CONNECT api.example.com:0 HTTP/1.1",
        "CONNECT api.example.com:99999 HTTP/1.1",
        "CONNECT api.example.com:https HTTP/1.1",
    ],
)
def test_a_head_that_is_not_a_request_is_refused(line: str) -> None:
    with pytest.raises(ProxyRequestInvalid):
        parse_head(head(line))


def test_a_line_that_is_not_a_header_is_refused() -> None:
    with pytest.raises(ProxyRequestInvalid):
        parse_head(head("CONNECT api.example.com:443 HTTP/1.1", "not a header"))


def test_the_forwarded_head_is_origin_form_without_the_hop_by_hop_headers() -> None:
    request = parse_head(
        head(
            "GET http://api.example.com/thing HTTP/1.1",
            "Host: api.example.com",
            "Authorization: Bearer target-credential",
            "Proxy-Authorization: Bearer platform-token",
            "Proxy-Connection: keep-alive",
            "Accept: application/json",
        )
    )

    forwarded = request.forwarded_head().decode()

    assert forwarded.startswith("GET /thing HTTP/1.1\r\n")
    # The target's own credential travels; the proxy's does not. Sending
    # `Proxy-Authorization` on would leak a platform token to every host a Run
    # talks to.
    assert "Authorization: Bearer target-credential" in forwarded
    assert "Proxy-Authorization" not in forwarded
    assert "Proxy-Connection" not in forwarded
    assert "Accept: application/json" in forwarded


def test_a_header_can_be_read_by_name_without_regard_to_case() -> None:
    request = parse_head(
        head("CONNECT api.example.com:443 HTTP/1.1", "proxy-authorization: Bearer t")
    )

    assert request.header("Proxy-Authorization") == "Bearer t"
    assert request.header("X-Absent") is None
