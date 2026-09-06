import hashlib
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.infrastructure.tables import RunRow, SessionRow
from tiny_hermes.session_workspace.domain.manifest import build_manifest
from tiny_hermes.session_workspace.domain.models import WorkspaceEntry
from tiny_hermes.session_workspace.infrastructure.minio_store import MinioObjectStore
from tiny_hermes.session_workspace.infrastructure.tables import WorkspaceRevisionRow
from tiny_hermes.session_workspace.ports.objects import blob_object, manifest_object
from tiny_hermes.shared.config import Settings


async def _chunks(body: bytes) -> AsyncIterator[bytes]:
    yield body


async def seed_files(
    engine: AsyncEngine,
    settings: Settings,
    run_id: str,
) -> tuple[UUID, bytes]:
    body = "# 已完成\n这是真实存储中的摘要。\n".encode()
    entry = WorkspaceEntry.file("notes/summary.md", body, mode=0o644)
    manifest = build_manifest((entry,), schema_version=1)
    revision_id = uuid4()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as db:
        run = (await db.execute(select(RunRow).where(RunRow.id == UUID(run_id)))).scalar_one()
        objects = MinioObjectStore(
            endpoint=settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            bucket=settings.s3_bucket,
        )
        await objects.ensure_bucket()
        await objects.put_stream(
            blob_object(
                workspace_id=run.workspace_id,
                session_id=run.session_id,
                digest=hashlib.sha256(body).hexdigest(),
            ),
            _chunks(body),
            limit_bytes=1024,
        )
        ref = manifest_object(
            workspace_id=run.workspace_id, session_id=run.session_id, revision_id=revision_id
        )
        encoded = manifest.canonical_bytes()
        await objects.put_stream(ref, _chunks(encoded), limit_bytes=4096)
        db.add(
            WorkspaceRevisionRow(
                id=revision_id,
                workspace_id=run.workspace_id,
                session_id=run.session_id,
                parent_revision_id=None,
                manifest_schema_version=1,
                manifest_object_key=ref.key,
                manifest_sha256=hashlib.sha256(encoded).hexdigest(),
                total_bytes=manifest.total_bytes,
                object_count=manifest.object_count,
                created_by_run_id=run.id,
            )
        )
        await db.flush()
        run.checkpoint_workspace_revision_id = revision_id
        await db.execute(
            update(SessionRow)
            .where(SessionRow.id == run.session_id)
            .values(workspace_revision_id=revision_id)
        )
    return revision_id, body
