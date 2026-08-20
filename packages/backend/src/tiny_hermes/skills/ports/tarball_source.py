"""Where an imported skill's files come from.

The port hands back files, not bytes, so that fetching and deframing stay on
the infrastructure side together: the catalog's rules have no reason to know
that a git host serves gzipped tar, and no reason to import either the outbound
face's errors or `tarfile` to say "that URL did not work".
"""

from dataclasses import dataclass
from typing import Protocol

from tiny_hermes.skills.domain.package import SkillFile


class TarballUnavailable(Exception):
    """The archive could not be fetched or could not be read, in words the
    person who typed the URL can act on. It never carries a resolved address: a
    refusal shown to a workspace member must not become a way to map the
    platform's network."""


@dataclass(frozen=True)
class FetchedTarball:
    files: tuple[SkillFile, ...]
    #: The immutable reference the server said this is — a commit sha or an
    #: ETag, read out of the response rather than out of the URL. "Imported from
    #: `main`" is not reproducible two months later; this is what was fetched.
    ref: str | None


class TarballSource(Protocol):
    async def fetch(self, url: str) -> FetchedTarball: ...
