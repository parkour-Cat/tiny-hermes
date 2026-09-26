"""Copy every object from one S3 endpoint to another, and check the bytes.

Written for one move: MinIO's images left Docker Hub on 2026-09-11 and the
bundled object store is SeaweedFS now. A deployment's artifacts, skill
packages and session workspaces sit in MinIO's own on-disk format, which
SeaweedFS cannot read, so they are copied over through the S3 API while the
old MinIO still runs. `docs/operations.md` has the step-by-step.

Every object is read back from the target and compared with the source by
SHA-256 — a copy that lands the right keys with the wrong bytes is the failure
worth catching, and a key count cannot see it. An object already on the
target with the same bytes is skipped, so a copy interrupted halfway is
finished by running it again.

Reads each object whole into memory. Fine for this platform's objects
(workspace revisions, artifacts and skill packages, each bounded by the
upload limits); not a general-purpose migration tool.

Usage::

    uv run --no-sync python scripts/migrate_object_store.py \\
      --source http://127.0.0.1:19000 --source-access-key ... --source-secret-key ... \\
      --target http://127.0.0.1:9000 --target-access-key ... --target-secret-key ...
"""

import argparse
import hashlib
import io
import sys
from dataclasses import dataclass, field
from urllib.parse import urlparse

from minio import Minio
from minio.error import S3Error


class MigrationFailed(RuntimeError):
    pass


@dataclass
class BucketReport:
    bucket: str
    copied: int = 0
    skipped: int = 0
    keys: list[str] = field(default_factory=list[str])


def _read(client: Minio, bucket: str, key: str) -> bytes | None:
    try:
        response = client.get_object(bucket, key)
    except S3Error as error:
        if error.code == "NoSuchKey":
            return None
        raise
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def copy_bucket(
    source: Minio, target: Minio, source_bucket: str, target_bucket: str
) -> BucketReport:
    """Copy ``source_bucket`` into ``target_bucket`` (created if missing),
    reading each copied object back to confirm its bytes."""
    if not target.bucket_exists(target_bucket):
        target.make_bucket(target_bucket)
    report = BucketReport(bucket=source_bucket)
    for entry in source.list_objects(source_bucket, recursive=True):
        key = entry.object_name
        if key is None:
            continue
        body = _read(source, source_bucket, key)
        if body is None:
            raise MigrationFailed(f"{source_bucket}/{key} was listed but could not be read")
        expected = hashlib.sha256(body).hexdigest()
        existing = _read(target, target_bucket, key)
        if existing is not None and hashlib.sha256(existing).hexdigest() == expected:
            report.skipped += 1
            continue
        target.put_object(target_bucket, key, io.BytesIO(body), len(body))
        landed = _read(target, target_bucket, key)
        if landed is None or hashlib.sha256(landed).hexdigest() != expected:
            raise MigrationFailed(f"{target_bucket}/{key} did not read back the same bytes")
        report.copied += 1
        report.keys.append(key)
    return report


def _client(endpoint: str, access_key: str, secret_key: str) -> Minio:
    parsed = urlparse(endpoint)
    return Minio(parsed.netloc, access_key, secret_key, secure=parsed.scheme == "https")


def main() -> int:
    parser = argparse.ArgumentParser(description="Copy all buckets between S3 endpoints")
    parser.add_argument("--source", required=True)
    parser.add_argument("--source-access-key", required=True)
    parser.add_argument("--source-secret-key", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--target-access-key", required=True)
    parser.add_argument("--target-secret-key", required=True)
    arguments = parser.parse_args()

    source = _client(arguments.source, arguments.source_access_key, arguments.source_secret_key)
    target = _client(arguments.target, arguments.target_access_key, arguments.target_secret_key)
    buckets = [bucket.name for bucket in source.list_buckets()]
    if not buckets:
        print("the source has no buckets; nothing to copy")
        return 0
    for bucket in buckets:
        report = copy_bucket(source, target, bucket, bucket)
        print(f"{bucket}: copied {report.copied}, already identical {report.skipped}")
    print("every object on the target was read back and matched its source")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except MigrationFailed as failed:
        print(f"migration failed: {failed}", file=sys.stderr)
        sys.exit(1)
