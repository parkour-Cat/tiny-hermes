from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.shared.config import Settings

from ..workspace_file_support import seed_files


async def test_persisted_files_can_be_listed_and_downloaded_without_creating_artifacts(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine,
    settings: Settings, submitted_run: dict[str, Any],
) -> None:
    run_id = str(submitted_run["id"])
    revision_id, body = await seed_files(engine, settings, run_id)
    base = f"/api/v1/runs/{run_id}/files"
    listed = client.get(base, headers=scope)
    assert listed.status_code == 200, listed.text
    assert listed.json()["revision_id"] == str(revision_id)
    assert listed.json()["items"][0]["path"] == "notes/summary.md"
    assert "object_key" not in listed.text
    downloaded = client.get(f"{base}/content", headers=scope,
                            params={"revision_id": str(revision_id), "path": "notes/summary.md"})
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == body
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    assert client.get(f"/api/v1/runs/{run_id}/artifacts", headers=scope).json() == []


async def test_a_download_cannot_choose_an_unlisted_path_or_another_snapshot(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine,
    settings: Settings, submitted_run: dict[str, Any],
) -> None:
    run_id = str(submitted_run["id"])
    revision_id, _ = await seed_files(engine, settings, run_id)
    base = f"/api/v1/runs/{run_id}/files/content"
    for path in ("../notes/summary.md", "/etc/passwd", "missing.md"):
        assert client.get(base, headers=scope, params={"revision_id": str(revision_id), "path": path}).status_code == 404
    assert client.get(base, headers=scope, params={"revision_id": str(uuid4()), "path": "notes/summary.md"}).status_code == 409
    wrong_scope = {**scope, "X-Workspace-Id": str(uuid4())}
    assert client.get(f"/api/v1/runs/{run_id}/files", headers=wrong_scope).status_code != 200
