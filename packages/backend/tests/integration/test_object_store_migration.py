"""The copy that moves a deployment's objects from MinIO to SeaweedFS.

MinIO's images left Docker Hub on 2026-09-11 and the bundled object store is
SeaweedFS now. A deployment's existing artifacts and skill packages sit in
MinIO's own on-disk format, which SeaweedFS cannot read, so upgrading means
copying them over through the S3 API while the old MinIO still runs.

What is checked is the **content** of every object after the copy, read back
from the target — the same standard `object_restore_drill.py` holds a restore
to. A copy that lands the right keys with the wrong bytes is the failure worth
catching, and a key count cannot see it.

Both ends here are the one S3 endpoint the integration suite already uses,
with two throwaway buckets: the logic under test is the copy and the check,
not a particular server.
"""

import hashlib
import importlib.util
import io
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from minio import Minio
from minio.deleteobjects import DeleteObject
from tiny_hermes.shared.config import Settings

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "migrate_object_store.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migrate_object_store", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def client(settings: Settings) -> Minio:
    endpoint = urlparse(settings.s3_endpoint)
    return Minio(
        endpoint.netloc,
        settings.s3_access_key,
        settings.s3_secret_key,
        secure=endpoint.scheme == "https",
    )


@pytest.fixture
def buckets(client: Minio) -> Iterator[tuple[str, str]]:
    names = (f"migrate-src-{uuid4().hex[:8]}", f"migrate-dst-{uuid4().hex[:8]}")
    for name in names:
        client.make_bucket(name)
    yield names
    for name in names:
        keys = [DeleteObject(o.object_name) for o in client.list_objects(name, recursive=True)]
        list(client.remove_objects(name, keys))
        client.remove_bucket(name)


#: A nested key, an empty object, and one past the 5 MiB point where the SDK
#: switches to multipart — the shapes a copy that only handled small flat
#: objects would get wrong.
OBJECTS = {
    "workspaces/a/revisions/0001.json": b'{"files": []}',
    "artifacts/empty": b"",
    "skills/pack.tar": hashlib.sha256(b"pack").digest() * (6 * 1024 * 1024 // 32 + 1),
}


def _seed(client: Minio, bucket: str) -> None:
    for key, body in OBJECTS.items():
        client.put_object(bucket, key, io.BytesIO(body), len(body))


def _read(client: Minio, bucket: str, key: str) -> bytes:
    response = client.get_object(bucket, key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def test_every_object_arrives_byte_for_byte(
    client: Minio, buckets: tuple[str, str]
) -> None:
    source, target = buckets
    _seed(client, source)

    report = _load().copy_bucket(client, client, source, target)

    assert report.copied == len(OBJECTS)
    assert report.skipped == 0
    for key, body in OBJECTS.items():
        assert _read(client, target, key) == body, key


def test_running_it_again_copies_nothing(client: Minio, buckets: tuple[str, str]) -> None:
    """Idempotent, so a copy interrupted halfway is finished by running it again."""
    source, target = buckets
    _seed(client, source)
    migrate = _load()
    migrate.copy_bucket(client, client, source, target)

    again = migrate.copy_bucket(client, client, source, target)

    assert again.copied == 0
    assert again.skipped == len(OBJECTS)


def test_a_target_object_with_other_bytes_is_replaced(
    client: Minio, buckets: tuple[str, str]
) -> None:
    """Same key, same size, different content: skipping it on size alone would
    leave the wrong bytes in place and report success."""
    source, target = buckets
    _seed(client, source)
    key = "workspaces/a/revisions/0001.json"
    wrong = b"X" * len(OBJECTS[key])
    client.put_object(target, key, io.BytesIO(wrong), len(wrong))

    report = _load().copy_bucket(client, client, source, target)

    assert _read(client, target, key) == OBJECTS[key]
    assert report.copied == len(OBJECTS)
