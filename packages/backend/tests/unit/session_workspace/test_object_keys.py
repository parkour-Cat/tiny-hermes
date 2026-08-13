"""Object keys are server-generated, tenant-scoped, and never a caller's string.

Design §6.5 and invariant 5: a model or API caller never supplies a key. The
builders here are the only way an ObjectRef comes to exist, and each one is
assembled from authenticated identifiers — so the test for "no traversal" is a
test of the only constructor there is.
"""

from uuid import UUID

import pytest
from tiny_hermes.session_workspace.domain.manifest import InvalidWorkspacePath
from tiny_hermes.session_workspace.ports.objects import (
    InvalidObjectDigest,
    artifact_object,
    blob_object,
    manifest_object,
    staging_object,
)

WORKSPACE = UUID("11111111-2222-4333-8444-555555555555")
SESSION = UUID("66666666-7777-4888-8999-aaaaaaaaaaaa")
RUN = UUID("99999999-8888-4777-a666-555555555555")
UPLOAD = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
REVISION = UUID("12121212-3434-4565-8787-909090909090")
ARTIFACT = UUID("fedcba98-7654-4321-8fed-cba987654321")
DIGEST = "d" * 64


def test_staging_keys_are_scoped_to_workspace_session_and_upload() -> None:
    ref = staging_object(
        workspace_id=WORKSPACE, session_id=SESSION, upload_id=UPLOAD, name="blobs/a"
    )
    assert ref.key == (
        f"workspaces/{WORKSPACE}/sessions/{SESSION}/staging/{UPLOAD}/blobs/a"
    )


def test_blob_keys_are_content_addressed_inside_one_session() -> None:
    ref = blob_object(workspace_id=WORKSPACE, session_id=SESSION, digest=DIGEST)
    assert ref.key == (
        f"workspaces/{WORKSPACE}/sessions/{SESSION}/blobs/sha256/{DIGEST}"
    )


def test_manifest_keys_are_determined_by_the_revision_id() -> None:
    ref = manifest_object(
        workspace_id=WORKSPACE, session_id=SESSION, revision_id=REVISION
    )
    assert ref.key == (
        f"workspaces/{WORKSPACE}/sessions/{SESSION}/manifests/{REVISION}.json"
    )


def test_artifact_keys_are_scoped_to_workspace_and_run() -> None:
    ref = artifact_object(workspace_id=WORKSPACE, run_id=RUN, artifact_id=ARTIFACT)
    assert ref.key == f"workspaces/{WORKSPACE}/runs/{RUN}/artifacts/{ARTIFACT}"


@pytest.mark.parametrize("name", ["/abs", "../up", "a/../b", "a\\b", "a\x00b", ""])
def test_a_staging_name_cannot_traverse(name: str) -> None:
    with pytest.raises(InvalidWorkspacePath):
        staging_object(
            workspace_id=WORKSPACE, session_id=SESSION, upload_id=UPLOAD, name=name
        )


@pytest.mark.parametrize("digest", ["", "abc", "Z" * 64, "d" * 63, "d" * 65])
def test_a_blob_digest_must_be_sixty_four_hex_characters(digest: str) -> None:
    with pytest.raises(InvalidObjectDigest):
        blob_object(workspace_id=WORKSPACE, session_id=SESSION, digest=digest)
