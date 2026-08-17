"""The catalog over HTTP, against the real database and the real router.

The unit tests next door say what `SkillCatalog` decides. These say that the
route hands it the right things and turns its answers into the right status
codes — in particular the 200/201 split on re-upload, which no service-level
test can see.
"""

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

SKILL_MD = """---
name: release-notes
description: Turn a changelog into release notes in this company's house style.
---

# Release notes

Read [the house style](style.md) and write one paragraph per kind of change.
"""


def _files(body: str = "Short sentences.") -> list[dict[str, str]]:
    return [
        {"path": "SKILL.md", "content": SKILL_MD},
        {"path": "style.md", "content": body},
    ]


def _create(
    client: TestClient,
    scope: dict[str, str],
    *,
    kind: str = "workspace",
    body: str = "Short sentences.",
) -> Any:
    return client.post(
        "/api/v1/skills", headers=scope, json={"scope": kind, "files": _files(body)}
    )


def test_a_directory_of_files_becomes_a_skill_and_its_first_version(
    client: TestClient, scope: dict[str, str]
) -> None:
    """Upload takes `[{path, content}]`. There is no endpoint here that would
    accept an archive, which is red line three on the manual path."""
    created = _create(client, scope)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "release-notes"
    assert body["scope"] == "workspace"
    assert body["current_version_id"] is not None

    versions = client.get(f"/api/v1/skills/{body['id']}/versions", headers=scope)
    assert versions.status_code == 200
    assert [item["version_number"] for item in versions.json()] == [1]
    assert versions.json()[0]["source"] == "upload"
    assert versions.json()[0]["bindable"] is True


def test_re_uploading_the_same_content_answers_200_and_publishes_nothing(
    client: TestClient, scope: dict[str, str]
) -> None:
    """The roadmap's exit check for re-import, spoken in HTTP."""
    skill_id = _create(client, scope).json()["id"]
    again = client.post(
        f"/api/v1/skills/{skill_id}/versions", headers=scope, json={"files": _files()}
    )
    assert again.status_code == 200
    assert again.json()["version_number"] == 1

    changed = client.post(
        f"/api/v1/skills/{skill_id}/versions",
        headers=scope,
        json={"files": _files("Shorter sentences.")},
    )
    assert changed.status_code == 201
    assert changed.json()["version_number"] == 2
    listed = client.get(f"/api/v1/skills/{skill_id}/versions", headers=scope)
    assert len(listed.json()) == 2


def test_a_second_skill_by_the_same_name_is_a_conflict(
    client: TestClient, scope: dict[str, str]
) -> None:
    assert _create(client, scope).status_code == 201
    clash = _create(client, scope, body="Shorter sentences.")
    assert clash.status_code == 409
    assert clash.json()["code"] == "skill_name_taken"


def test_credentials_in_a_file_refuse_the_upload_and_name_the_file(
    client: TestClient, scope: dict[str, str]
) -> None:
    refused = _create(client, scope, body="Use AKIAIOSFODNN7EXAMPLE for the API.\n")
    assert refused.status_code == 422
    problem = refused.json()
    assert problem["code"] == "skill_scan_refused"
    findings = problem["context"]["findings"]
    assert [finding["path"] for finding in findings] == ["style.md"]
    assert findings[0]["severity"] == "blocking"
    assert client.get("/api/v1/skills", headers=scope).json() == []


def test_files_without_a_manifest_are_not_a_package(
    client: TestClient, scope: dict[str, str]
) -> None:
    refused = client.post(
        "/api/v1/skills",
        headers=scope,
        json={"scope": "workspace", "files": [{"path": "style.md", "content": "x"}]},
    )
    assert refused.status_code == 422
    assert refused.json()["code"] == "invalid_skill_package"


def test_a_version_read_carries_the_bodies_and_a_listing_does_not(
    client: TestClient, scope: dict[str, str]
) -> None:
    skill = _create(client, scope).json()
    listed = client.get(f"/api/v1/skills/{skill['id']}/versions", headers=scope).json()
    assert "files" not in listed[0]
    read = client.get(
        f"/api/v1/skills/{skill['id']}/versions/{skill['current_version_id']}",
        headers=scope,
    )
    assert read.status_code == 200
    assert {item["path"] for item in read.json()["files"]} == {"SKILL.md", "style.md"}


def test_withdrawing_the_default_clears_it_and_keeps_the_content(
    client: TestClient, scope: dict[str, str]
) -> None:
    skill = _create(client, scope).json()
    version_id = skill["current_version_id"]
    withdrawn = client.post(
        f"/api/v1/skills/{skill['id']}/versions/{version_id}/withdraw", headers=scope
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["status"] == "withdrawn"
    assert withdrawn.json()["bindable"] is False
    after = client.get(f"/api/v1/skills/{skill['id']}", headers=scope)
    assert after.json()["current_version_id"] is None
    read = client.get(
        f"/api/v1/skills/{skill['id']}/versions/{version_id}", headers=scope
    )
    assert read.status_code == 200


def test_the_default_rolls_back_and_refuses_a_withdrawn_version(
    client: TestClient, scope: dict[str, str]
) -> None:
    skill = _create(client, scope).json()
    first = skill["current_version_id"]
    second = client.post(
        f"/api/v1/skills/{skill['id']}/versions",
        headers=scope,
        json={"files": _files("Shorter sentences.")},
    ).json()["id"]
    moved = client.put(
        f"/api/v1/skills/{skill['id']}/current-version",
        headers=scope,
        json={"version_id": second},
    )
    assert moved.status_code == 200
    assert moved.json()["current_version_id"] == second
    rolled_back = client.put(
        f"/api/v1/skills/{skill['id']}/current-version",
        headers=scope,
        json={"version_id": first},
    )
    assert rolled_back.json()["current_version_id"] == first

    client.post(
        f"/api/v1/skills/{skill['id']}/versions/{second}/withdraw", headers=scope
    )
    refused = client.put(
        f"/api/v1/skills/{skill['id']}/current-version",
        headers=scope,
        json={"version_id": second},
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "skill_version_not_bindable"


async def test_a_viewer_may_read_and_may_not_upload(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, workspace_id: str
) -> None:
    skill = _create(client, scope).json()
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE memberships SET role = 'viewer' WHERE workspace_id = :id"),
            {"id": workspace_id},
        )
    assert client.get("/api/v1/skills", headers=scope).status_code == 200
    refused = client.post(
        f"/api/v1/skills/{skill['id']}/versions",
        headers=scope,
        json={"files": _files("Shorter sentences.")},
    )
    assert refused.status_code == 403


async def test_a_platform_skill_is_readable_from_a_workspace_and_not_writable(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, workspace_id: str
) -> None:
    """§15.1's built-in skills. The admin who makes one is a platform admin;
    the workspace administrator who reads it is not."""
    created = _create(client, scope, kind="platform")
    assert created.status_code == 201
    assert created.json()["workspace_id"] is None
    skill_id = created.json()["id"]

    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE users SET is_platform_admin = false"),
        )
    listed = client.get("/api/v1/skills", headers=scope)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [skill_id]
    refused = client.post(
        f"/api/v1/skills/{skill_id}/versions",
        headers=scope,
        json={"files": _files("Shorter sentences.")},
    )
    assert refused.status_code == 403


async def test_another_workspace_s_skill_is_not_found_rather_than_forbidden(
    client: TestClient, scope: dict[str, str], admin_csrf: str
) -> None:
    """Which skills exist elsewhere is exactly what an outsider may not learn."""
    skill_id = _create(client, scope).json()["id"]
    other = client.post(
        "/api/v1/workspaces",
        headers={"X-CSRF-Token": admin_csrf},
        json={"name": "other"},
    )
    assert other.status_code == 201
    elsewhere = {"X-Workspace-Id": other.json()["id"], "X-CSRF-Token": admin_csrf}
    assert client.get("/api/v1/skills", headers=elsewhere).json() == []
    missing = client.get(f"/api/v1/skills/{skill_id}", headers=elsewhere)
    assert missing.status_code == 404
    assert missing.json()["code"] == "skill_not_found"


async def test_creating_a_skill_writes_one_audit_row(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine
) -> None:
    assert _create(client, scope).status_code == 201
    async with engine.connect() as connection:
        actions = (
            await connection.execute(
                text("SELECT action FROM audit_events WHERE action LIKE 'skill.%'")
            )
        ).scalars().all()
    assert list(actions) == ["skill.created"]
