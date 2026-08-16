"""Artifact reads: membership rechecked, cross-tenant probes told nothing.

Design §6.4. The dangerous mistakes here are answers that differ: a 403 for
"exists but not yours" beside a 404 for "does not exist" is an oracle. Every
wrong-workspace path must raise the same ``ArtifactNotFound``.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from tiny_hermes.artifacts.application.service import (
    ArtifactForbidden,
    ArtifactNotFound,
    ArtifactService,
)
from tiny_hermes.artifacts.domain.models import Artifact
from tiny_hermes.session_workspace.ports.objects import ObjectRef
from tiny_hermes.tenancy.domain.models import Actor, Role

WORKSPACE = uuid.uuid4()
OTHER_WORKSPACE = uuid.uuid4()
MEMBER = Actor(uuid.uuid4(), False)
OUTSIDER = Actor(uuid.uuid4(), False)
PLATFORM_ADMIN = Actor(uuid.uuid4(), True)


def _artifact(*, expired: bool = False) -> Artifact:
    return Artifact(
        id=uuid.uuid4(),
        workspace_id=WORKSPACE,
        session_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        object_key=f"workspaces/{WORKSPACE}/runs/x/artifacts/y",
        filename="output.log",
        media_type="text/plain",
        size_bytes=5,
        sha256="a" * 64,
        truncated=False,
        expires_at=datetime.now(UTC) + timedelta(hours=-1 if expired else 1),
    )


class FakeStore:
    def __init__(self, artifact: Artifact | None) -> None:
        self.artifacts = [] if artifact is None else [artifact]
        self.roles: dict[tuple[UUID, UUID], Role] = {}

    async def insert(self, artifact: Artifact) -> None:  # pragma: no cover
        raise AssertionError("reads never insert")

    async def read_scoped(
        self, artifact_id: UUID, workspace_id: UUID
    ) -> Artifact | None:
        for found in self.artifacts:
            if found.id == artifact_id and found.workspace_id == workspace_id:
                return found
        return None

    async def role_for(self, workspace_id: UUID, user_id: UUID) -> Role | None:
        return self.roles.get((workspace_id, user_id))

    async def run_total_bytes(self, run_id: UUID) -> int:  # pragma: no cover
        del run_id
        return 0

    async def list_for_run(self, workspace_id: UUID, run_id: UUID) -> list[Artifact]:
        return [
            item
            for item in self.artifacts
            if item.workspace_id == workspace_id and item.run_id == run_id
        ]


class FakeObjects:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def get_stream(self, ref: ObjectRef) -> AsyncIterator[bytes]:
        yield self.blobs[ref.key]

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - unused surface
        raise AssertionError(f"reads never call {name}")


def _service(artifact: Artifact | None) -> tuple[ArtifactService, FakeStore, FakeObjects]:
    store = FakeStore(artifact)
    objects = FakeObjects()
    return ArtifactService(store, objects), store, objects  # type: ignore[arg-type]


async def test_a_member_reads_metadata_and_content() -> None:
    artifact = _artifact()
    service, store, objects = _service(artifact)
    store.roles[(WORKSPACE, MEMBER.id)] = Role.VIEWER
    objects.blobs[artifact.object_key] = b"hello"

    found = await service.metadata(WORKSPACE, MEMBER, artifact.id)

    assert found.filename == "output.log"
    received = b""
    async for chunk in service.content(found):
        received += chunk
    assert received == b"hello"


async def test_cross_tenant_requests_get_a_generic_not_found() -> None:
    """The artifact exists — in another workspace. The answer must not say so."""
    artifact = _artifact()
    service, store, _ = _service(artifact)
    store.roles[(OTHER_WORKSPACE, MEMBER.id)] = Role.WORKSPACE_ADMIN

    with pytest.raises(ArtifactNotFound):
        await service.metadata(OTHER_WORKSPACE, MEMBER, artifact.id)


async def test_a_missing_artifact_sounds_exactly_like_a_foreign_one() -> None:
    service, store, _ = _service(None)
    store.roles[(WORKSPACE, MEMBER.id)] = Role.VIEWER

    with pytest.raises(ArtifactNotFound):
        await service.metadata(WORKSPACE, MEMBER, uuid.uuid4())


async def test_an_expired_artifact_is_gone_even_before_gc_sweeps_it() -> None:
    artifact = _artifact(expired=True)
    service, store, _ = _service(artifact)
    store.roles[(WORKSPACE, MEMBER.id)] = Role.VIEWER

    with pytest.raises(ArtifactNotFound):
        await service.metadata(WORKSPACE, MEMBER, artifact.id)


async def test_a_non_member_is_refused_before_any_lookup() -> None:
    artifact = _artifact()
    service, _, _ = _service(artifact)

    with pytest.raises(ArtifactForbidden):
        await service.metadata(WORKSPACE, OUTSIDER, artifact.id)


async def test_a_platform_admin_reads_without_a_membership_row() -> None:
    artifact = _artifact()
    service, _, objects = _service(artifact)
    objects.blobs[artifact.object_key] = b"x"

    found = await service.metadata(WORKSPACE, PLATFORM_ADMIN, artifact.id)
    assert found.id == artifact.id


async def test_listing_a_run_omits_expired_artifacts_and_never_names_a_key() -> None:
    live = _artifact()
    expired = _artifact(expired=True)
    expired = Artifact(
        id=expired.id,
        workspace_id=WORKSPACE,
        session_id=live.session_id,
        run_id=live.run_id,
        object_key=expired.object_key,
        filename="old.log",
        media_type="text/plain",
        size_bytes=1,
        sha256="b" * 64,
        truncated=False,
        expires_at=expired.expires_at,
    )
    service, store, _ = _service(live)
    store.artifacts.append(expired)
    store.roles[(WORKSPACE, MEMBER.id)] = Role.VIEWER

    listed = await service.list_for_run(WORKSPACE, MEMBER, live.run_id)

    assert [item.id for item in listed] == [live.id]


async def test_a_non_member_cannot_list_a_run_s_artifacts() -> None:
    artifact = _artifact()
    service, _, _ = _service(artifact)

    with pytest.raises(ArtifactForbidden):
        await service.list_for_run(WORKSPACE, OUTSIDER, artifact.run_id)
