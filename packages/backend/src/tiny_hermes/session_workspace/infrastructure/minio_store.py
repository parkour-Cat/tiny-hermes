"""The only module that constructs a MinIO client.

The SDK is synchronous, so every call goes through ``asyncio.to_thread`` — the
same trade the Docker engine made, for the same reason: the calls are network
round-trips measured in milliseconds, and this repository is developed on
Windows where the async alternatives are not.

Streaming without buffering: ``put_stream`` bridges the caller's async iterator
into the SDK's blocking ``read()`` with ``run_coroutine_threadsafe``, so the
bytes flow chunk by chunk from the event loop into the uploading thread. At no
point does a whole workspace exist in this process's memory, which the design
(§16.4) measures rather than takes on faith.
"""

import asyncio
import hashlib
import io
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast
from urllib.parse import urlparse

from tiny_hermes.session_workspace.ports.objects import (
    ObjectMissing,
    ObjectRef,
    ObjectStat,
    ObjectStorageUnavailable,
    ObjectTooLarge,
    StoredObject,
)

#: The SDK requires a part size when the total length is unknown. 16 MiB keeps
#: multipart bookkeeping small without holding more than one part in flight.
PART_SIZE = 16 * 1024 * 1024
READ_CHUNK = 1024 * 1024


class MinioObjectStore:
    def __init__(
        self, *, endpoint: str, access_key: str, secret_key: str, bucket: str
    ) -> None:
        from minio import Minio  # noqa: PLC0415 - the one construction point

        parsed = urlparse(endpoint)
        if not parsed.netloc:
            raise ValueError(f"s3_endpoint needs a scheme and host: {endpoint}")
        self._client = Minio(
            parsed.netloc,
            access_key=access_key,
            secret_key=secret_key,
            secure=parsed.scheme == "https",
        )
        self._bucket = bucket

    async def ensure_bucket(self) -> None:
        def make() -> None:
            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)

        await self._call(make)

    async def put_stream(
        self, ref: ObjectRef, chunks: AsyncIterator[bytes], *, limit_bytes: int
    ) -> StoredObject:
        loop = asyncio.get_running_loop()
        reader = _AsyncChunkReader(loop=loop, chunks=chunks, limit_bytes=limit_bytes)

        def upload() -> None:
            self._client.put_object(
                self._bucket, ref.key, cast(Any, reader), length=-1, part_size=PART_SIZE
            )

        try:
            await self._call(upload)
        except ObjectTooLarge:
            raise
        return StoredObject(size=reader.total, sha256=reader.digest())

    async def get_stream(self, ref: ObjectRef) -> AsyncIterator[bytes]:
        from minio.error import S3Error  # noqa: PLC0415

        def open_object() -> Any:
            try:
                return self._client.get_object(self._bucket, ref.key)
            except S3Error as failure:
                if failure.code in ("NoSuchKey", "NoSuchObject"):
                    raise ObjectMissing(ref.key) from failure
                raise

        response = await self._call(open_object)
        try:
            while True:
                chunk: bytes = await asyncio.to_thread(response.read, READ_CHUNK)
                if not chunk:
                    return
                yield chunk
        finally:
            response.close()
            response.release_conn()

    async def stat(self, ref: ObjectRef) -> ObjectStat | None:
        from minio.error import S3Error  # noqa: PLC0415

        def ask() -> ObjectStat | None:
            try:
                found = self._client.stat_object(self._bucket, ref.key)
            except S3Error as missing:
                if missing.code in ("NoSuchKey", "NoSuchObject"):
                    return None
                raise
            return ObjectStat(size=int(found.size or 0))

        return await self._call(ask)

    async def server_copy(self, source: ObjectRef, target: ObjectRef) -> None:
        from minio.commonconfig import CopySource  # noqa: PLC0415

        def copy() -> None:
            self._client.copy_object(
                self._bucket, target.key, CopySource(self._bucket, source.key)
            )

        await self._call(copy)

    async def delete_many(self, refs: Sequence[ObjectRef]) -> None:
        from minio.deleteobjects import DeleteObject  # noqa: PLC0415

        def delete() -> list[str]:
            errors = self._client.remove_objects(
                self._bucket, [DeleteObject(ref.key) for ref in refs]
            )
            return [str(error) for error in errors]

        failed = await self._call(delete)
        if failed:
            # A deletion that did not happen must stay visible — cleanup marks
            # itself pending rather than believing it finished.
            raise ObjectStorageUnavailable(f"objects not deleted: {failed[:3]}")

    async def list_prefix(self, prefix: str, *, limit: int) -> tuple[ObjectRef, ...]:
        def enumerate_keys() -> tuple[ObjectRef, ...]:
            found: list[ObjectRef] = []
            for entry in self._client.list_objects(
                self._bucket, prefix=prefix, recursive=True
            ):
                name = entry.object_name
                if name is None:
                    continue
                found.append(ObjectRef(key=name))
                if len(found) >= limit:
                    break
            return tuple(found)

        return await self._call(enumerate_keys)

    async def _call(self, work: Any) -> Any:
        from minio.error import MinioException  # noqa: PLC0415
        from urllib3.exceptions import HTTPError  # noqa: PLC0415

        try:
            return await asyncio.to_thread(work)
        except (ObjectTooLarge, ObjectMissing):
            raise
        except (MinioException, HTTPError, OSError) as failure:
            raise ObjectStorageUnavailable(str(failure)) from failure


class _AsyncChunkReader(io.RawIOBase):
    """A blocking file the SDK can read, fed by an async iterator.

    Runs inside the upload thread; each ``readinto`` asks the event loop for
    the next chunk. Counting and hashing happen here so the caller gets the
    size and digest of what was actually sent, measured once.
    """

    def __init__(
        self, *, loop: asyncio.AbstractEventLoop, chunks: AsyncIterator[bytes], limit_bytes: int
    ) -> None:
        super().__init__()
        self._loop = loop
        self._chunks = chunks
        self._limit = limit_bytes
        self._pending = b""
        self._hasher = hashlib.sha256()
        self.total = 0

    def digest(self) -> str:
        return self._hasher.hexdigest()

    def readable(self) -> bool:
        return True

    def readinto(self, target: Any) -> int:
        view = memoryview(target)
        if not self._pending:
            fetched = asyncio.run_coroutine_threadsafe(
                anext(self._chunks, None), self._loop
            ).result()
            if fetched is None:
                return 0
            self.total += len(fetched)
            if self.total > self._limit:
                raise ObjectTooLarge(f"stream exceeded {self._limit} bytes")
            self._hasher.update(fetched)
            self._pending = fetched
        taken = min(len(view), len(self._pending))
        view[:taken] = self._pending[:taken]
        self._pending = self._pending[taken:]
        return taken
