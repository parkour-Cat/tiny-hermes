"""Binding a real skill to a real Agent, over HTTP and against the database.

The unit tests say what the publish check decides. These say that the two
catalogs actually meet: the skill created through one router is the version the
other router's publish check reads, through the adapter the resources wire in.
Nothing but an end-to-end test can see that wiring.
"""

from typing import Any

from fastapi.testclient import TestClient

from ..conftest import VALID_SPEC

SKILL_MD = """---
name: house-style
description: How this company writes, in one paragraph per kind of change.
---

# House style

Lead with what changed for the reader.
"""


def _skill(client: TestClient, scope: dict[str, str]) -> dict[str, Any]:
    created = client.post(
        "/api/v1/skills",
        headers=scope,
        json={
            "scope": "workspace",
            "files": [
                {"path": "SKILL.md", "content": SKILL_MD},
                {"path": "style.md", "content": "Short sentences."},
            ],
        },
    )
    assert created.status_code == 201, created.text
    skill = created.json()
    versions = client.get(f"/api/v1/skills/{skill['id']}/versions", headers=scope)
    return {"skill": skill, "version": versions.json()[0]}


def _draft(
    client: TestClient, scope: dict[str, str], bindings: list[dict[str, str]]
) -> tuple[str, int]:
    agent_id = str(
        client.post(
            "/api/v1/agents", headers=scope, json={"name": "Writer", "alias": "writer"}
        ).json()["id"]
    )
    draft = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": {**VALID_SPEC, "skills": bindings}},
    )
    assert draft.status_code == 200, draft.text
    return agent_id, int(draft.json()["revision"])


def test_an_agent_publishes_with_a_skill_bound_by_version(
    client: TestClient, scope: dict[str, str]
) -> None:
    imported = _skill(client, scope)
    agent_id, revision = _draft(
        client, scope, [{"skill_version_id": imported["version"]["id"]}]
    )

    published = client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=scope,
        json={"expected_revision": revision},
    )

    assert published.status_code == 201, published.text
    assert len(published.json()["content_hash"]) == 64


def test_withdrawing_the_bound_version_stops_the_next_publication(
    client: TestClient, scope: dict[str, str]
) -> None:
    """The Agent published before the withdrawal keeps its version and keeps
    working — §15.1 is explicit that stopping a version stops new bindings, not
    running Runs. What it stops is the publish after it."""
    imported = _skill(client, scope)
    binding = [{"skill_version_id": imported["version"]["id"]}]
    agent_id, revision = _draft(client, scope, binding)
    first = client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=scope,
        json={"expected_revision": revision},
    )
    assert first.status_code == 201

    withdrawn = client.post(
        f"/api/v1/skills/{imported['skill']['id']}/versions/"
        f"{imported['version']['id']}/withdraw",
        headers=scope,
    )
    assert withdrawn.status_code == 200, withdrawn.text
    changed = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={
            "expected_revision": revision,
            "spec": {**VALID_SPEC, "skills": binding, "personality": "You are brief."},
        },
    )
    assert changed.status_code == 200
    refused = client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=scope,
        json={"expected_revision": changed.json()["revision"]},
    )

    assert refused.status_code == 422, refused.text
    problem = refused.json()
    assert problem["code"] == "skill_binding_unavailable"
    assert problem["context"]["bindings"][0]["skill_version_id"] == (
        imported["version"]["id"]
    )
    assert "withdrawn" in problem["context"]["bindings"][0]["reason"]


def test_another_workspace_s_version_is_not_bindable_or_nameable(
    client: TestClient, scope: dict[str, str], admin_csrf: str
) -> None:
    """The refusal says the version is not visible, not that it is elsewhere."""
    imported = _skill(client, scope)
    other = client.post(
        "/api/v1/workspaces", headers={"X-CSRF-Token": admin_csrf}, json={"name": "Other"}
    )
    assert other.status_code == 201
    elsewhere = {"X-Workspace-Id": other.json()["id"], "X-CSRF-Token": admin_csrf}
    agent_id, revision = _draft(
        client, elsewhere, [{"skill_version_id": imported["version"]["id"]}]
    )

    refused = client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=elsewhere,
        json={"expected_revision": revision},
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "skill_binding_unavailable"
    assert "visible" in refused.json()["context"]["bindings"][0]["reason"]


def test_a_platform_skill_is_bindable_from_a_workspace(
    client: TestClient, scope: dict[str, str], admin_csrf: str
) -> None:
    """Read-only from a workspace, which does not mean unusable from one."""
    created = client.post(
        "/api/v1/skills",
        headers=scope,
        json={
            "scope": "platform",
            "files": [{"path": "SKILL.md", "content": SKILL_MD}],
        },
    )
    assert created.status_code == 201, created.text
    version = client.get(
        f"/api/v1/skills/{created.json()['id']}/versions", headers=scope
    ).json()[0]
    agent_id, revision = _draft(client, scope, [{"skill_version_id": version["id"]}])

    published = client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=scope,
        json={"expected_revision": revision},
    )

    assert published.status_code == 201, published.text
