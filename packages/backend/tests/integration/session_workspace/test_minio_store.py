"""The object adapter against a real MinIO, not a mock of one.

The compose file provides the server (`docker compose up -d minio`); the suite
skips with a reason when it is absent, the same shape as the sandbox suite and
its Docker daemon. Every test writes under a unique prefix and deletes what it
wrote in `finally`, because a shared local MinIO full of leftovers is a cost
somebody pays later.
"""

import hashlib
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from tiny_hermes.session_workspace.infrastructure.minio_store import MinioObjectStore
from tiny_hermes.session_workspace.ports.objects import (
    ObjectRef,
    ObjectTooLarge,
    staging_object,
)

WORKSPACE = uuid.uuid4()
SESSION = uuid.uuid4()


@pytest.fixture(scope="module")
def store() -> MinioObjectStore:
    value = MinioObjectStore(
        endpoint=os.environ.get("S3_ENDPOINT", "http://localhost:9000"),
        access_key=os.environ.get("S3_ACCESS_KEY", "tiny-hermes-local"),
        secret_key=os.environ.get("S3_SECRET_KEY", "tiny-hermes-local-password"),
        bucket=os.environ.get("S3_BUCKET", "tiny-hermes-test"),
    )
    return value


@pytest.fixture(autouse=True)
async def reachable(store: MinioObjectStore) -> None:
    import socket
    from urllib.parse import urlparse

    from tiny_hermes.session_workspace.ports.objects import ObjectStorageUnavailable

    # A fast probe first: the SDK's own retries take minutes to conclude what
    # one TCP connect can say in a second.
    parsed = urlparse(os.environ.get("S3_ENDPOINT", "http://localhost:9000"))
    try:
        socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or 9000), timeout=1
        ).close()
    except OSError as unreachable:  # pragma: no cover - environment
        pytest.skip(f"no reachable MinIO: {unreachable}")
    try:
        await store.ensure_bucket()
    except ObjectStorageUnavailable as unreachable:  # pragma: no cover - environment
        pytest.skip(f"no reachable MinIO: {unreachable}")


def _ref(name: str) -> ObjectRef:
    return staging_object(
        workspace_id=WORKSPACE, session_id=SESSION, upload_id=uuid.uuid4(), name=name
    )


async def _chunks(parts: list[bytes]) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def test_put_stream_measures_what_it_sent(store: MinioObjectStore) -> None:
    parts = [b"a" * 1000, b"b" * 500, b"c" * 1]
    body = b"".join(parts)
    ref = _ref("measured")
    try:
        stored = await store.put_stream(ref, _chunks(parts), limit_bytes=10_000)
        assert stored.size == len(body)
        assert stored.sha256 == hashlib.sha256(body).hexdigest()
        stat = await store.stat(ref)
        assert stat is not None and stat.size == len(body)
    finally:
        await store.delete_many([ref])


async def test_round_trip_streams_back_the_same_bytes(store: MinioObjectStore) -> None:
    # Larger than one read chunk, so the loop actually loops.
    body = os.urandom(3 * 1024 * 1024)
    ref = _ref("round-trip")
    try:
        await store.put_stream(ref, _chunks([body]), limit_bytes=len(body))
        received = b""
        async for chunk in store.get_stream(ref):
            received += chunk
        assert hashlib.sha256(received).hexdigest() == hashlib.sha256(body).hexdigest()
    finally:
        await store.delete_many([ref])


async def test_a_stream_over_its_limit_is_refused(store: MinioObjectStore) -> None:
    ref = _ref("over-limit")
    with pytest.raises(ObjectTooLarge):
        await store.put_stream(ref, _chunks([b"x" * 100, b"y" * 1]), limit_bytes=100)


async def test_server_copy_moves_bytes_without_this_process(store: MinioObjectStore) -> None:
    source, target = _ref("copy-source"), _ref("copy-target")
    try:
        await store.put_stream(source, _chunks([b"payload"]), limit_bytes=100)
        await store.server_copy(source, target)
        stat = await store.stat(target)
        assert stat is not None and stat.size == len(b"payload")
    finally:
        await store.delete_many([source, target])


async def test_stat_of_a_missing_object_is_none_not_an_error(
    store: MinioObjectStore,
) -> None:
    assert await store.stat(_ref("never-written")) is None


async def test_delete_many_removes_everything_it_names(store: MinioObjectStore) -> None:
    refs = [_ref(f"gone-{index}") for index in range(3)]
    for ref in refs:
        await store.put_stream(ref, _chunks([b"x"]), limit_bytes=10)
    await store.delete_many(refs)
    for ref in refs:
        assert await store.stat(ref) is None
