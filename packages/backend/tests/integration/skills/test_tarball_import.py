"""Importing a skill from a server, over a real connection.

The tar reader's refusals are settled member by member in the unit tests. What
those cannot show is the part that only exists once bytes are on a socket: that
the fetch goes through `SafeOutboundClient` and therefore inherits its address
policy, so a tarball URL that redirects to link-local space is refused without
this module containing a rule about it.

Two things are relaxed here, both explicitly and both local to this file:
loopback becomes reachable, and `http` becomes an importable scheme — a stand-in
on this machine has nowhere to live and no certificate to present. Neither
relaxation can hide a mistake in the code under test, because both are named
collaborators the production wiring passes its own values for.
"""

import asyncio
import contextlib
import gzip
import io
import os
import tarfile
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
import uvicorn
from tiny_hermes.outbound.client import EgressRoute, SafeOutboundClient
from tiny_hermes.skills.infrastructure.outbound_tarball import OutboundTarballSource
from tiny_hermes.skills.infrastructure.tarball import (
    MAX_MEMBER_BYTES,
    MAX_MEMBERS,
    MAX_TOTAL_BYTES,
)
from tiny_hermes.skills.ports.tarball_source import TarballUnavailable

from ..egress_support import PROXY_TOKEN, ProxyHandle, running_proxy

SKILL_MD = """---
name: release-notes
description: Turn a changelog into release notes in this company's house style.
---

# Release notes

Lead with what changed for the reader.
"""

#: Where a redirect points when a test wants the outbound face to say no. Cloud
#: metadata is the address every SSRF write-up ends at, so it is the one worth
#: naming in a test.
METADATA = "http://169.254.169.254/latest/meta-data/"


def tarball(
    files: dict[str, bytes] | None = None,
    *,
    extra: list[tarfile.TarInfo] | None = None,
    root: str = "house-style-9f1c2ab",
) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for path, body in (files or {"SKILL.md": SKILL_MD.encode()}).items():
            info = tarfile.TarInfo(name=f"{root}/{path}" if root else path)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
        for info in extra or []:
            tar.addfile(info)
    return raw.getvalue()


@dataclass
class Host:
    """A server that answers with whatever the test handed it."""

    body: bytes = b""
    status: int = 200
    disposition: str | None = None
    etag: str | None = None
    paths: list[str] = field(default_factory=list[str])

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        assert scope["type"] == "http"
        path = str(scope["path"])
        query = scope["query_string"].decode("latin-1")
        self.paths.append(path)
        while True:
            if not (await receive()).get("more_body", False):
                break

        headers: list[tuple[bytes, bytes]] = []
        status, body = self.status, self.body
        if path == "/redirect":
            status, body = 302, b""
            headers.append((b"location", query.removeprefix("to=").encode("latin-1")))
        else:
            headers.append((b"content-type", b"application/gzip"))
            if self.disposition is not None:
                headers.append((b"content-disposition", self.disposition.encode()))
            if self.etag is not None:
                headers.append((b"etag", self.etag.encode()))
        headers.append((b"content-length", str(len(body)).encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


@contextlib.asynccontextmanager
async def _serving(app: Host) -> AsyncGenerator[str]:
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(1_000):
            if server.started:
                break
            await asyncio.sleep(0.01)
        address: Any = server.servers[0].sockets[0].getsockname()
        yield f"http://127.0.0.1:{int(address[1])}"
    finally:
        server.should_exit = True
        await task


@pytest.fixture
async def host() -> AsyncIterator[tuple[Host, str]]:
    app = Host()
    async with _serving(app) as url:
        yield app, url


@pytest.fixture
async def proxy() -> AsyncIterator[ProxyHandle]:
    """The boundary this import has to cross, like every other outbound call."""
    async with running_proxy() as handle:
        yield handle


def source(
    proxy: ProxyHandle, *, max_response_bytes: int = 16 * 1024 * 1024
) -> OutboundTarballSource:
    def client() -> SafeOutboundClient:
        return SafeOutboundClient(
            egress=EgressRoute(url=proxy.url, token=PROXY_TOKEN),
            connect_timeout=2.0,
            read_timeout=10.0,
            max_redirects=3,
            max_response_bytes=max_response_bytes,
        )

    return OutboundTarballSource(client, schemes=frozenset({"http", "https"}))


async def test_a_tarball_on_a_socket_becomes_skill_files(
    host: tuple[Host, str], proxy: ProxyHandle
) -> None:
    app, url = host
    app.body = tarball({"SKILL.md": SKILL_MD.encode(), "style.md": b"Short sentences."})
    app.disposition = 'attachment; filename=house-style-9f1c2ab0e7d4.tar.gz'

    fetched = await source(proxy).fetch(f"{url}/archive/main.tar.gz")

    assert {entry.path for entry in fetched.files} == {"SKILL.md", "style.md"}
    assert fetched.ref == "9f1c2ab0e7d4"
    assert app.paths == ["/archive/main.tar.gz"]


async def test_the_reference_falls_back_to_the_etag(
    host: tuple[Host, str], proxy: ProxyHandle
) -> None:
    """`source_ref` is a courtesy, not a guarantee — `content_hash` already
    pins the bytes. So the weaker reference is used when it is all there is."""
    app, url = host
    app.body = tarball()
    app.etag = 'W/"e5f6a7b8"'

    fetched = await source(proxy).fetch(f"{url}/archive.tar.gz")

    assert fetched.ref == "e5f6a7b8"


async def test_a_redirect_to_link_local_space_is_refused_by_the_outbound_face(
    host: tuple[Host, str], proxy: ProxyHandle
) -> None:
    """The reason imports go through the outbound client at all.

    The first hop is a host somebody was allowed to name; the second is the one
    they actually wanted. Nothing in the import code inspects the target — the
    refusal comes from the per-hop re-resolution, which is why it is right.
    """
    app, url = host
    app.body = tarball()

    with pytest.raises(TarballUnavailable, match="not one this platform will call"):
        await source(proxy).fetch(f"{url}/redirect?to={METADATA}")

    assert app.paths == ["/redirect"], "the second hop was made anyway"


async def test_a_symlink_member_arriving_over_the_wire_is_refused(
    host: tuple[Host, str], proxy: ProxyHandle
) -> None:
    app, url = host
    link = tarfile.TarInfo(name="house-style-9f1c2ab/passwd")
    link.type = tarfile.SYMTYPE
    link.linkname = "../../../../etc/passwd"
    app.body = tarball(extra=[link])

    with pytest.raises(TarballUnavailable, match="not a regular file"):
        await source(proxy).fetch(f"{url}/archive.tar.gz")


async def test_too_many_members_are_refused(
    host: tuple[Host, str], proxy: ProxyHandle
) -> None:
    app, url = host
    app.body = tarball({f"file-{index}.md": b"x" for index in range(MAX_MEMBERS + 1)})

    with pytest.raises(TarballUnavailable, match=f"more than {MAX_MEMBERS} members"):
        await source(proxy).fetch(f"{url}/archive.tar.gz")


async def test_one_member_over_its_ceiling_is_refused(
    host: tuple[Host, str], proxy: ProxyHandle
) -> None:
    app, url = host
    app.body = tarball({"big.md": os.urandom(MAX_MEMBER_BYTES).hex().encode()})

    with pytest.raises(TarballUnavailable, match="is larger than"):
        await source(proxy).fetch(f"{url}/archive.tar.gz")


async def test_members_adding_up_past_the_ceiling_are_refused(
    host: tuple[Host, str], proxy: ProxyHandle
) -> None:
    app, url = host
    body = os.urandom(MAX_MEMBER_BYTES // 2 - 1).hex().encode()
    count = MAX_TOTAL_BYTES // len(body) + 2
    app.body = tarball({f"file-{index}.md": body for index in range(count)})

    with pytest.raises(TarballUnavailable, match="unpacks to more than"):
        await source(proxy).fetch(f"{url}/archive.tar.gz")


async def test_an_archive_that_expands_too_far_is_refused(
    host: tuple[Host, str], proxy: ProxyHandle
) -> None:
    """Small on the wire, large in memory. The response ceiling cannot see
    this one coming, which is why the reader has a ratio of its own."""
    app, url = host
    zeros = b"\0" * (MAX_MEMBER_BYTES - 1)
    app.body = tarball({f"file-{index}.md": zeros for index in range(3)})
    assert len(app.body) < 64 * 1024, "the bomb should be cheap to serve"

    with pytest.raises(TarballUnavailable, match="expands more than"):
        await source(proxy).fetch(f"{url}/archive.tar.gz")


async def test_an_archive_past_the_response_ceiling_never_reaches_the_reader(
    host: tuple[Host, str], proxy: ProxyHandle
) -> None:
    """The outbound face stops first, and says so in the import's words."""
    app, url = host
    app.body = tarball({"notes.md": os.urandom(60_000).hex().encode()})

    with pytest.raises(TarballUnavailable, match="too large to import"):
        await source(proxy, max_response_bytes=4096).fetch(f"{url}/archive.tar.gz")


async def test_a_page_that_is_not_a_tarball_is_refused(
    host: tuple[Host, str], proxy: ProxyHandle
) -> None:
    """A private repository answers a login page with `200`, not `404`."""
    app, url = host
    app.body = b"<!doctype html><title>Sign in</title>"

    with pytest.raises(TarballUnavailable, match="could not be read"):
        await source(proxy).fetch(f"{url}/archive.tar.gz")


async def test_a_status_that_is_not_two_hundred_is_refused(
    host: tuple[Host, str], proxy: ProxyHandle
) -> None:
    app, url = host
    app.status = 404
    app.body = b"nope"

    with pytest.raises(TarballUnavailable, match="answered 404"):
        await source(proxy).fetch(f"{url}/archive.tar.gz")


async def test_the_wired_source_imports_over_https_only() -> None:
    """The relaxation above is the test's, not the platform's."""
    def unusable() -> SafeOutboundClient:  # pragma: no cover - never called
        raise AssertionError("the scheme should have been refused first")

    with pytest.raises(TarballUnavailable, match="only be imported over HTTPS"):
        await OutboundTarballSource(unusable).fetch("http://example.com/x.tar.gz")


def test_the_bomb_in_these_tests_really_is_one() -> None:
    """Guards the test above: if gzip stopped compressing zeros, that test
    would pass against a reader with no ratio ceiling at all."""
    zeros = b"\0" * (MAX_MEMBER_BYTES - 1)
    assert len(gzip.compress(zeros)) * 100 < len(zeros)
