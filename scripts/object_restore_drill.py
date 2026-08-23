"""Back up the object store, lose it, put it back — and check the bytes.

`docs/operations.md` says artifacts and skill packages live in MinIO and
must be backed up separately, and then recorded that this had never been
rehearsed. The database drills next to it found two things a runbook cannot
tell you by being read (`--data-only` restores in the wrong order; rolling
back destroys the deduplication record), so the object half deserved the
same treatment rather than a sentence.

What this checks that a byte count would not: the **content** of every
object after the restore. A restore that returns the right number of
objects with the wrong bytes in them is the failure worth catching, and it
is invisible to `mc ls`.

The drill works in a bucket it creates and deletes. It never touches the
bucket named by `S3_BUCKET`, because a restore drill rehearsing on live
artifacts would be the accident it exists to prevent.

Usage::

    docker compose -f deploy/compose/compose.yaml up -d minio --wait
    uv run --no-sync python scripts/object_restore_drill.py \\
      --endpoint http://127.0.0.1:9000 \\
      --access-key tiny-hermes-local --secret-key tiny-hermes-local-password
"""

import argparse
import hashlib
import io
import json
import sys
from typing import Any
from urllib.parse import urlparse

#: What the drill writes, and then expects back byte for byte. Sizes chosen
#: to cross the boundary where a client may switch to multipart, because a
#: restore that only ever handled small objects proves nothing about the
#: ones that matter.
OBJECTS: tuple[tuple[str, int], ...] = (
    ("runs/a/small.txt", 32),
    ("runs/a/medium.bin", 512 * 1024),
    ("skills/pack.tar", 6 * 1024 * 1024),
)


class DrillFailed(RuntimeError):
    pass


def _payload(name: str, size: int) -> bytes:
    """Deterministic, and different per object — identical bodies would let a
    restore that mixed two objects up still pass."""
    seed = hashlib.sha256(name.encode()).digest()
    return (seed * (size // len(seed) + 1))[:size]


def main() -> int:
    parser = argparse.ArgumentParser(description="Object store backup/restore drill")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--access-key", required=True)
    parser.add_argument("--secret-key", required=True)
    parser.add_argument("--bucket", default="tiny-hermes-restore-drill")
    parser.add_argument("--backup", default="tiny-hermes-restore-drill-backup")
    parsed = parser.parse_args()


    from minio import Minio  # noqa: PLC0415 - a drill, not the platform

    # `argparse` hands back `Any`, so the conversions are what let a type
    # checker see this at all — and the drill is the wrong place to be the
    # one file nobody can check.
    endpoint = str(parsed.endpoint)
    live = str(parsed.bucket)
    backup_bucket = str(parsed.backup)
    location = urlparse(endpoint)
    client = Minio(
        location.netloc,
        access_key=str(parsed.access_key),
        secret_key=str(parsed.secret_key),
        secure=location.scheme == "https",
    )

    for each in (live, backup_bucket):
        if client.bucket_exists(each):
            for existing in client.list_objects(each, recursive=True):
                client.remove_object(each, str(existing.object_name))
            client.remove_bucket(each)
        client.make_bucket(each)

    digests: dict[str, str] = {}
    for name, size in OBJECTS:
        body = _payload(name, size)
        digests[name] = hashlib.sha256(body).hexdigest()
        client.put_object(live, name, io.BytesIO(body), size)

    # The backup: every object copied out, which is what an operator's
    # `mc mirror` does under a friendlier name.
    for name, _ in OBJECTS:
        data = client.get_object(live, name)
        try:
            body = data.read()
        finally:
            data.close()
            data.release_conn()
        client.put_object(backup_bucket, name, io.BytesIO(body), len(body))

    # The loss. Not a deleted bucket — a live one that lost its contents,
    # which is the shape most incidents actually take.
    #
    # `live` spelled out rather than reusing the loop variable above: an
    # earlier version did reuse it, which by then held the *backup* bucket,
    # so the drill emptied its own backup and then asked why the restore
    # found nothing. A drill that clears the wrong bucket is testing itself
    # rather than the runbook.
    for existing in client.list_objects(live, recursive=True):
        client.remove_object(live, str(existing.object_name))
    remaining = list(client.list_objects(live, recursive=True))

    for name, _ in OBJECTS:
        data = client.get_object(backup_bucket, name)
        try:
            body = data.read()
        finally:
            data.close()
            data.release_conn()
        client.put_object(live, name, io.BytesIO(body), len(body))

    findings: list[dict[str, Any]] = []
    for name, size in OBJECTS:
        data = client.get_object(live, name)
        try:
            body = data.read()
        finally:
            data.close()
            data.release_conn()
        findings.append(
            {
                "object": name,
                "bytes": len(body),
                "expected_bytes": size,
                # The whole point: identical content, not merely present.
                "content_matches": hashlib.sha256(body).hexdigest() == digests[name],
            }
        )

    print(
        json.dumps(
            {
                "objects_after_loss": len(remaining),
                "restored": findings,
            },
            indent=2,
        )
    )

    for each in (live, backup_bucket):
        for existing in client.list_objects(each, recursive=True):
            client.remove_object(each, str(existing.object_name))
        client.remove_bucket(each)

    intact = all(entry["content_matches"] for entry in findings)
    if intact and not remaining:
        print("\nEvery object came back byte for byte, and the loss was real.")
        return 0
    sys.stderr.write("\nThe restore did not return what was backed up.\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
