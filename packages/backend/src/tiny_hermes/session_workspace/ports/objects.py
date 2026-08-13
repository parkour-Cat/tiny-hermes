"""Where workspace bytes live, named only by the platform.

Invariant 5 of the session-workspace design: object-store keys are generated
from authenticated Workspace, Session, and server-side identifiers. The four
builders below are the only way an ``ObjectRef`` comes to exist, so "a caller
never supplies a key" is a property of this module's exports rather than a
check scattered through callers.

The port is deliberately bounded: every read and write states its limit up
front, and nothing returns an unbounded byte string.
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from tiny_hermes.session_workspace.domain.manifest import normalize_workspace_path
from tiny_hermes.session_workspace.domain.models import DIGEST_HEX_LENGTH


class InvalidObjectDigest(Exception):
    """A digest that is not sixty-four lowercase hex characters."""


class ObjectTooLarge(Exception):
    """A stream that exceeded the limit its operation declared."""


class ObjectStorageUnavailable(Exception):
    """The object store could not be spoken to.

    Distinct from a missing object: this is the platform being unable to ask,
    which design §7 maps to ``workspace_storage_unavailable`` and a bounded
    recovery rather than a failed Run.
    """


@dataclass(frozen=True)
class ObjectRef:
    """One fully-formed object key. Constructed only by the builders below."""

    key: str


def staging_object(
    *, workspace_id: UUID, session_id: UUID, upload_id: UUID, name: str
) -> ObjectRef:
    """A key under one registered upload's staging prefix.

    ``name`` is server-generated too, but it is validated anyway — the path
    rules are one function, and an exemption for "trusted" names is how a
    trusted name eventually is not.
    """
    safe = normalize_workspace_path(name)
    return ObjectRef(
        key=f"workspaces/{workspace_id}/sessions/{session_id}/staging/{upload_id}/{safe}"
    )


def blob_object(*, workspace_id: UUID, session_id: UUID, digest: str) -> ObjectRef:
    """A content-addressed body, deduplicated inside one Session only.

    Session-scoped rather than global — design §6.5 — so the existence or
    timing of a cross-tenant blob can never leak what another tenant stored.
    """
    if len(digest) != DIGEST_HEX_LENGTH or any(c not in "0123456789abcdef" for c in digest):
        raise InvalidObjectDigest(digest[:16])
    return ObjectRef(
        key=f"workspaces/{workspace_id}/sessions/{session_id}/blobs/sha256/{digest}"
    )


def manifest_object(
    *, workspace_id: UUID, session_id: UUID, revision_id: UUID
) -> ObjectRef:
    return ObjectRef(
        key=f"workspaces/{workspace_id}/sessions/{session_id}/manifests/{revision_id}.json"
    )


def artifact_object(*, workspace_id: UUID, run_id: UUID, artifact_id: UUID) -> ObjectRef:
    return ObjectRef(key=f"workspaces/{workspace_id}/runs/{run_id}/artifacts/{artifact_id}")


@dataclass(frozen=True)
class StoredObject:
    """What a completed upload turned out to be, measured during the stream."""

    size: int
    sha256: str


@dataclass(frozen=True)
class ObjectStat:
    size: int


class ObjectStore(Protocol):
    """The bounded object-store contract the SessionWorkspace module uses.

    Implementations stream; none may hold a whole workspace in memory, which
    the performance gate in the design (§16.4) measures rather than trusts.
    """

    async def put_stream(
        self, ref: ObjectRef, chunks: AsyncIterator[bytes], *, limit_bytes: int
    ) -> StoredObject: ...

    def get_stream(self, ref: ObjectRef) -> AsyncIterator[bytes]: ...

    async def stat(self, ref: ObjectRef) -> ObjectStat | None: ...

    async def server_copy(self, source: ObjectRef, target: ObjectRef) -> None: ...

    async def delete_many(self, refs: Sequence[ObjectRef]) -> None: ...
