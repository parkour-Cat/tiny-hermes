"""Reclaiming a registered upload's objects, in the one safe order.

Design §8: read and verify the candidate index, recheck GC roots, delete
unreferenced final keys, delete staging, delete the index, and only then mark
the row settled. The index is deleted last among objects because it is the
only complete enumeration of what a failed commit may have placed — cleanup
that deletes its own map before finishing cannot be retried.

Failure here is an outcome, not an exception: a deletion that did not happen
leaves ``cleanup_pending`` set and the claim retryable, which is the whole
point of recording the debt in the database.
"""

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, cast
from uuid import UUID

from tiny_hermes.session_workspace.domain.models import UploadStatus
from tiny_hermes.session_workspace.ports.objects import (
    ObjectMissing,
    ObjectRef,
    ObjectStorageUnavailable,
    ObjectStore,
)
from tiny_hermes.session_workspace.ports.store import ObjectUpload, WorkspaceStore

INDEX_SCHEMA_VERSION = 1
#: An index enumerates keys and digests, not content; far beyond this it is
#: not an index but a mistake.
INDEX_LIMIT_BYTES = 1024 * 1024
#: How many staged objects one reclaim pass will enumerate and delete.
STAGING_BATCH = 1000


class Reference(StrEnum):
    """What the collector knows about one final key's other users."""

    REFERENCED = "referenced"
    UNREFERENCED = "unreferenced"
    UNCERTAIN = "uncertain"


class ReferenceOracle(Protocol):
    """Answers whether another GC root still needs a final key.

    ``UNCERTAIN`` is an honest first-class answer: design §13, an uncertain
    reference keeps the blob.
    """

    async def referenced(self, key: str) -> Reference: ...


class CandidateIndexInvalid(Exception):
    """The index failed verification and must not be believed."""


@dataclass(frozen=True)
class CandidateIndex:
    """The durable enumeration of one upload's intended final keys."""

    upload_id: UUID
    final_keys: tuple[str, ...]


def encode_candidate_index(index: CandidateIndex) -> bytes:
    return json.dumps(
        {
            "schema_version": INDEX_SCHEMA_VERSION,
            "upload_id": str(index.upload_id),
            "final_keys": list(index.final_keys),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def parse_candidate_index(data: bytes, *, expected_upload_id: UUID) -> CandidateIndex:
    try:
        parsed: Any = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as broken:
        raise CandidateIndexInvalid("not a JSON document") from broken
    if not isinstance(parsed, dict):
        raise CandidateIndexInvalid("not a JSON object")
    document = cast(dict[str, object], parsed)
    if document.get("schema_version") != INDEX_SCHEMA_VERSION:
        raise CandidateIndexInvalid(f"schema {document.get('schema_version')!r}")
    if document.get("upload_id") != str(expected_upload_id):
        raise CandidateIndexInvalid("index names a different upload")
    raw_keys = document.get("final_keys")
    if not isinstance(raw_keys, list):
        raise CandidateIndexInvalid("final_keys must be a list of keys")
    keys: list[str] = []
    for key in cast(list[object], raw_keys):
        if not isinstance(key, str):
            raise CandidateIndexInvalid("final_keys must be a list of keys")
        keys.append(key)
    return CandidateIndex(upload_id=expected_upload_id, final_keys=tuple(keys))


@dataclass(frozen=True)
class CleanupOutcome:
    upload_id: UUID
    finished: bool
    deleted_objects: int
    retry_reason: str | None = None


async def reclaim_upload(
    upload: ObjectUpload,
    *,
    store: WorkspaceStore,
    objects: ObjectStore,
    oracle: ReferenceOracle,
) -> CleanupOutcome:
    """One reclaim pass over one claimed upload. Safe to repeat."""
    try:
        return await _reclaim(upload, store=store, objects=objects, oracle=oracle)
    except ObjectStorageUnavailable as outage:
        # The row keeps its debt; the next claim retries the same work.
        return CleanupOutcome(
            upload_id=upload.upload_id,
            finished=False,
            deleted_objects=0,
            retry_reason=f"storage_unavailable: {outage}",
        )


async def _reclaim(
    upload: ObjectUpload,
    *,
    store: WorkspaceStore,
    objects: ObjectStore,
    oracle: ReferenceOracle,
) -> CleanupOutcome:
    deleted = 0
    index_ref = ObjectRef(key=upload.candidate_index_key)

    if upload.status is not UploadStatus.COMMITTED:
        # Final keys may exist for any candidate that reached `finalizing`.
        # The index is the only enumeration of them; read it before touching
        # anything.
        try:
            index = await _read_index(upload, objects)
        except CandidateIndexInvalid as broken:
            return CleanupOutcome(
                upload_id=upload.upload_id,
                finished=False,
                deleted_objects=0,
                retry_reason=f"index_invalid: {broken}",
            )
        if index is not None:
            doomed, uncertain = await _resolve_finals(index, oracle)
            if doomed:
                await objects.delete_many(doomed)
                deleted += len(doomed)
            if uncertain:
                # Deleting staging or the index now would strand the retry
                # this outcome asks for.
                return CleanupOutcome(
                    upload_id=upload.upload_id,
                    finished=False,
                    deleted_objects=deleted,
                    retry_reason=f"uncertain_reference: {uncertain} keys",
                )

    deleted += await _drain_staging(upload, objects)
    await objects.delete_many([index_ref])
    await store.finish_cleanup(upload.upload_id)
    return CleanupOutcome(
        upload_id=upload.upload_id, finished=True, deleted_objects=deleted + 1
    )


async def _read_index(
    upload: ObjectUpload, objects: ObjectStore
) -> CandidateIndex | None:
    """The verified index, or None when it verifiably never outlived cleanup.

    A missing index means either the upload never reached `finalizing` (no
    final key was ever written) or a previous pass already deleted the final
    keys and the index; both make final-key work unnecessary.
    """
    try:
        data = await _read_bounded(upload, objects)
    except ObjectMissing:
        return None
    index = parse_candidate_index(data, expected_upload_id=upload.upload_id)
    if upload.candidate_index_sha256 is not None:
        if hashlib.sha256(data).hexdigest() != upload.candidate_index_sha256:
            raise CandidateIndexInvalid("index bytes do not match the recorded digest")
    prefix = f"workspaces/{upload.workspace_id}/"
    for key in index.final_keys:
        if not key.startswith(prefix):
            raise CandidateIndexInvalid("index names a key outside this workspace")
    return index


async def _read_bounded(upload: ObjectUpload, objects: ObjectStore) -> bytes:
    received = b""
    async for chunk in objects.get_stream(ObjectRef(key=upload.candidate_index_key)):
        received += chunk
        if len(received) > INDEX_LIMIT_BYTES:
            raise CandidateIndexInvalid("index larger than any honest index")
    return received


async def _resolve_finals(
    index: CandidateIndex, oracle: ReferenceOracle
) -> tuple[list[ObjectRef], int]:
    """Split the enumerated final keys into deletable and still-in-doubt."""
    doomed: list[ObjectRef] = []
    uncertain = 0
    for key in index.final_keys:
        verdict = await oracle.referenced(key)
        if verdict is Reference.UNREFERENCED:
            doomed.append(ObjectRef(key=key))
        elif verdict is Reference.UNCERTAIN:
            uncertain += 1
    return doomed, uncertain


async def _drain_staging(upload: ObjectUpload, objects: ObjectStore) -> int:
    removed = 0
    while True:
        batch = await objects.list_prefix(upload.staging_prefix, limit=STAGING_BATCH)
        if not batch:
            return removed
        await objects.delete_many(batch)
        removed += len(batch)
