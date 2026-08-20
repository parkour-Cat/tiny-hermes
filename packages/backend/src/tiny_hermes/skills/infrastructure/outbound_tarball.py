"""Fetching a skill tarball through the one way out of this process.

Roadmap §5: an import's outbound request goes through the mandatory outbound
face of the day. That is not a stylistic preference — it means the address
policy, the per-hop re-resolution, the DNS pinning and the response ceiling all
apply to imports without a single rule being written twice here. A redirect from
a public host to `169.254.169.254` is refused by `SafeOutboundClient`, and this
module contains nothing that could have got that wrong.
"""

import re
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

from tiny_hermes.outbound.client import SafeOutboundClient
from tiny_hermes.outbound.errors import (
    OutboundError,
    OutboundRefused,
    OutboundTooLarge,
    OutboundTooManyRedirects,
)
from tiny_hermes.skills.infrastructure.tarball import TarballRefused, read_tarball
from tiny_hermes.skills.ports.tarball_source import FetchedTarball, TarballUnavailable

#: `codeload.github.com` names the file `<repo>-<sha>.tar.gz`, and that sha is
#: the most immutable reference anything in the response carries.
_SHA = re.compile(r"\b([0-9a-f]{40}|[0-9a-f]{7,12})\b")


#: What an import may be fetched over. The outbound face allows plaintext inside
#: an approved range; an import does not need it, and a skill fetched over HTTP
#: is a skill anyone on the path could have written.
SCHEMES = frozenset({"https"})


class OutboundTarballSource:
    def __init__(
        self,
        client: Callable[[], SafeOutboundClient],
        *,
        schemes: frozenset[str] = SCHEMES,
    ) -> None:
        # A factory, not an instance: a client that outlives a request is a
        # client whose approved ranges were read at a different time than they
        # are being used. `ApplicationResources.outbound_client` says the same.
        self._client = client
        # A collaborator for the same reason `SafeOutboundClient` takes its
        # policy as one: a stand-in server on this machine has nowhere to get a
        # certificate, and the alternative is leaving this path untested.
        self._schemes = schemes

    async def fetch(self, url: str) -> FetchedTarball:
        if urlsplit(url).scheme.lower() not in self._schemes:
            raise TarballUnavailable("A skill can only be imported over HTTPS.")
        try:
            async with self._client() as client:
                response = await client.request("GET", url)
        except OutboundRefused as error:
            raise TarballUnavailable(
                "That address is not one this platform will call."
            ) from error
        except OutboundTooLarge as error:
            raise TarballUnavailable("That archive is too large to import.") from error
        except OutboundTooManyRedirects as error:
            raise TarballUnavailable("That address redirects too many times.") from error
        except OutboundError as error:
            raise TarballUnavailable("That address could not be reached.") from error
        if response.status_code != 200:
            raise TarballUnavailable(
                f"That address answered {response.status_code}, not a tarball."
            )
        try:
            files = read_tarball(response.content)
        except TarballRefused as error:
            raise TarballUnavailable(str(error)) from error
        return FetchedTarball(files=files, ref=_reference(response.headers))


def _reference(headers: httpx.Headers) -> str | None:
    """The commit sha if the response names one, else the ETag, else nothing.

    A missing reference is not a failure: the version's `content_hash` already
    pins exactly which bytes were imported. `source_ref` is the extra courtesy
    of being able to find them again at the source.
    """
    disposition = headers.get("content-disposition") or ""
    found = _SHA.search(disposition)
    if found is not None:
        return found.group(1)
    etag = (headers.get("etag") or "").strip()
    if etag.startswith("W/"):
        etag = etag[2:]
    etag = etag.strip('"')
    return etag or None
