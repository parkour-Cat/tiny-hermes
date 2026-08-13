"""Artifacts end to end: recorded through the upload chain, downloaded twice-checked.

Real PostgreSQL and MinIO. The recorder registers before the first byte and
commits the row with the upload mark; the routes then serve exactly what a
member of the right workspace may see, and nothing to anybody else.
"""

import os
import socket
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tiny_hermes.artifacts.application.service import (
    ArtifactLimitExceeded,
    ArtifactLimits,
    ArtifactRecorder,
)
from tiny_hermes.session_workspace.domain.models import UploadStatus
from tiny_hermes.session_workspace.infrastructure.minio_store import MinioObjectStore
from tiny_hermes.session_workspace.infrastructure.sql_store import SqlWorkspaceStore

LIMITS = ArtifactLimits(
    artifact_max_bytes=1024,
    run_artifact_max_bytes=2048,
    retention_seconds=3600,
    staging_ttl_seconds=3600,
)


@pytest.fixture(scope="module")
def objects() -> MinioObjectStore:
    return MinioObjectStore(
        endpoint=os.environ.get("S3_ENDPOINT", "http://localhost:9000"),
        access_key=os.environ.get("S3_ACCESS_KEY", "tiny-hermes-local"),
        secret_key=os.environ.get("S3_SECRET_KEY", "tiny-hermes-local-password"),
        bucket=os.environ.get("S3_BUCKET", "tiny-hermes-test"),
    )


@pytest.fixture(autouse=True)
async def reachable(objects: MinioObjectStore) -> None:
    parsed = urlparse(os.environ.get("S3_ENDPOINT", "http://localhost:9000"))
    try:
        socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or 9000), timeout=1
        ).close()
    except OSError as unreachable:  # pragma: no cover - environment
        pytest.skip(f"no reachable MinIO: {unreachable}")
    await objects.ensure_bucket()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def _recorder(
    sessions: async_sessionmaker[AsyncSession],
    objects: MinioObjectStore,
    workspace_id: str,
    session_id: str,
    run_id: str,
    *,
    limits: ArtifactLimits = LIMITS,
) -> ArtifactRecorder:
    return ArtifactRecorder(
        sessions=sessions,
        objects=objects,
        workspace_id=UUID(workspace_id),
        session_id=UUID(session_id),
        run_id=UUID(run_id),
        filename="command-output.log",
        media_type="text/plain",
        limits=limits,
    )


async def test_recorded_output_downloads_byte_for_byte(
    client: TestClient,
    scope: dict[str, str],
    sessions: async_sessionmaker[AsyncSession],
    objects: MinioObjectStore,
    workspace_id: str,
    session_id: str,
    submitted_run: dict[str, Any],
) -> None:
    body = b"line one\nline two\n" * 20
    recorder = _recorder(
        sessions, objects, workspace_id, session_id, str(submitted_run["id"])
    )
    for start in range(0, len(body), 100):
        await recorder.deliver(body[start : start + 100])
    artifact = await recorder.finish(truncated=False)
    assert artifact is not None

    shown = client.get(f"/api/v1/artifacts/{artifact.id}", headers=scope)
    assert shown.status_code == 200
    assert shown.json()["size_bytes"] == len(body)
    assert shown.json()["truncated"] is False

    downloaded = client.get(f"/api/v1/artifacts/{artifact.id}/content", headers=scope)
    assert downloaded.status_code == 200
    assert downloaded.content == body

    async with sessions() as db:
        upload = await SqlWorkspaceStore(db).read(recorder.upload_id)
    assert upload is not None
    assert upload.status is UploadStatus.COMMITTED
    assert not upload.cleanup_pending, "staging was settled after the commit"

    listed = client.get(f"/api/v1/runs/{submitted_run['id']}/artifacts", headers=scope)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [str(artifact.id)]
    assert "object_key" not in listed.json()[0]
    assert listed.json()[0]["filename"] == "command-output.log"


async def test_cross_tenant_artifact_requests_get_a_generic_not_found(
    client: TestClient,
    scope: dict[str, str],
    admin_csrf: str,
    sessions: async_sessionmaker[AsyncSession],
    objects: MinioObjectStore,
    workspace_id: str,
    session_id: str,
    submitted_run: dict[str, Any],
) -> None:
    recorder = _recorder(
        sessions, objects, workspace_id, session_id, str(submitted_run["id"])
    )
    await recorder.deliver(b"private bytes")
    artifact = await recorder.finish(truncated=False)
    assert artifact is not None

    other = client.post(
        "/api/v1/workspaces",
        headers={"X-CSRF-Token": admin_csrf},
        json={"name": "Elsewhere"},
    ).json()["id"]

    foreign = client.get(
        f"/api/v1/artifacts/{artifact.id}",
        headers={"X-Workspace-Id": str(other), "X-CSRF-Token": admin_csrf},
    )
    missing = client.get(f"/api/v1/artifacts/{uuid4()}", headers=scope)

    assert foreign.status_code == 404
    assert missing.status_code == 404
    assert foreign.json()["code"] == missing.json()["code"], (
        "a cross-tenant probe must sound exactly like a missing artifact"
    )


async def test_per_artifact_and_per_run_ceilings_are_enforced(
    sessions: async_sessionmaker[AsyncSession],
    objects: MinioObjectStore,
    workspace_id: str,
    session_id: str,
    submitted_run: dict[str, Any],
) -> None:
    run_id = str(submitted_run["id"])

    over = _recorder(sessions, objects, workspace_id, session_id, run_id)
    with pytest.raises(ArtifactLimitExceeded):
        await over.deliver(b"x" * (LIMITS.artifact_max_bytes + 1))
        await over.finish(truncated=True)
    async with sessions() as db:
        abandoned = await SqlWorkspaceStore(db).read(over.upload_id)
    assert abandoned is not None
    assert abandoned.status is UploadStatus.ABANDONED

    first = _recorder(sessions, objects, workspace_id, session_id, run_id)
    await first.deliver(b"y" * 1024)
    assert await first.finish(truncated=False) is not None

    tight = ArtifactLimits(
        artifact_max_bytes=1024,
        run_artifact_max_bytes=1024,
        retention_seconds=3600,
        staging_ttl_seconds=3600,
    )
    second = _recorder(
        sessions, objects, workspace_id, session_id, run_id, limits=tight
    )
    with pytest.raises(ArtifactLimitExceeded):
        await second.deliver(b"z")


async def test_a_recorder_that_got_no_bytes_leaves_nothing_behind(
    sessions: async_sessionmaker[AsyncSession],
    objects: MinioObjectStore,
    workspace_id: str,
    session_id: str,
    submitted_run: dict[str, Any],
) -> None:
    recorder = _recorder(
        sessions, objects, workspace_id, session_id, str(submitted_run["id"])
    )
    assert await recorder.finish(truncated=False) is None
    async with sessions() as db:
        row = await SqlWorkspaceStore(db).read(recorder.upload_id)
    assert row is None, "no byte, no registration"

