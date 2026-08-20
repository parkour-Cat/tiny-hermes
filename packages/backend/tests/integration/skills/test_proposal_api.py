"""§15.3 over HTTP, and the one thing approval must not do.

The unit tests say what the catalog decides. This says the routes and the
database agree with it — and it ends on the sentence the whole stage is for: an
approved proposal produces a new version, and the Agent that proposed it keeps
running the old one until somebody republishes.
"""

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..conftest import VALID_SPEC

SKILL_MD = """---
name: rollout
description: How this company takes a machine out of rotation before a deploy.
---

# Rollout

Take the machine out of the pool first, then drain it.
"""

IMPROVED_MD = SKILL_MD.replace(
    "Take the machine out of the pool first, then drain it.",
    "Check the dashboard first. Take the machine out of the pool, then drain it.",
)

A_LEAKED_KEY = "aws_secret_access_key = AKIAIOSFODNN7EXAMPLE"


def _skill(client: TestClient, scope: dict[str, str]) -> dict[str, Any]:
    created = client.post(
        "/api/v1/skills",
        headers=scope,
        json={"scope": "workspace", "files": [{"path": "SKILL.md", "content": SKILL_MD}]},
    )
    assert created.status_code == 201, created.text
    skill = created.json()
    versions = client.get(f"/api/v1/skills/{skill['id']}/versions", headers=scope).json()
    return {"skill": skill, "version": versions[0]}


def _propose(
    client: TestClient,
    scope: dict[str, str],
    files: list[dict[str, str]],
    skill_id: str | None = None,
) -> dict[str, Any]:
    created = client.post(
        "/api/v1/skill-proposals",
        headers=scope,
        json={"files": files, "skill_id": skill_id},
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


def test_a_proposal_carries_the_diff_a_reviewer_decides_from(
    client: TestClient, scope: dict[str, str]
) -> None:
    existing = _skill(client, scope)
    proposal = _propose(
        client,
        scope,
        [{"path": "SKILL.md", "content": IMPROVED_MD}],
        existing["skill"]["id"],
    )

    read = client.get(f"/api/v1/skill-proposals/{proposal['id']}", headers=scope)

    assert read.status_code == 200, read.text
    body = read.json()
    assert body["status"] == "pending"
    assert body["approvable"] is True
    assert body["base_version_id"] == existing["version"]["id"]
    changed = body["diff"][0]
    assert changed["path"] == "SKILL.md"
    assert changed["change"] == "changed"
    assert changed["added_lines"] == 1
    assert changed["removed_lines"] == 1
    assert any("Check the dashboard" in line["text"] for line in changed["lines"])


def test_an_unapproved_proposal_adds_no_version(
    client: TestClient, scope: dict[str, str]
) -> None:
    existing = _skill(client, scope)
    _propose(
        client,
        scope,
        [{"path": "SKILL.md", "content": IMPROVED_MD}],
        existing["skill"]["id"],
    )

    versions = client.get(
        f"/api/v1/skills/{existing['skill']['id']}/versions", headers=scope
    ).json()

    assert [item["id"] for item in versions] == [existing["version"]["id"]]


def test_approving_publishes_a_version_and_leaves_the_binding_alone(
    client: TestClient, scope: dict[str, str]
) -> None:
    """The sentence this whole stage exists for.

    An Agent published against version 1 still runs version 1 after the
    approval, because an AgentSpec names a version id. Nothing here had to
    check that — it is what binding by id means — which is exactly why it is
    pinned: a later change that "helpfully" repointed the Agent on approval
    would break §15.3's last rule with nothing else failing.
    """
    existing = _skill(client, scope)
    agent_id = str(
        client.post(
            "/api/v1/agents", headers=scope, json={"name": "Runner", "alias": "runner"}
        ).json()["id"]
    )
    draft = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={
            "expected_revision": 1,
            "spec": {
                **VALID_SPEC,
                "skills": [{"skill_version_id": existing["version"]["id"]}],
            },
        },
    )
    published = client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=scope,
        json={"expected_revision": draft.json()["revision"]},
    )
    assert published.status_code == 201, published.text
    proposal = _propose(
        client,
        scope,
        [{"path": "SKILL.md", "content": IMPROVED_MD}],
        existing["skill"]["id"],
    )

    approved = client.post(
        f"/api/v1/skill-proposals/{proposal['id']}/approve", headers=scope
    )

    assert approved.status_code == 201, approved.text
    version = approved.json()
    assert version["version_number"] == 2
    assert version["source"] == "proposal"
    assert version["source_ref"] == proposal["id"]
    # The Agent still names version 1, and the skill still offers version 1 to
    # whoever binds it next. Switching is a republish somebody performs.
    agent = client.get(f"/api/v1/agents/{agent_id}", headers=scope).json()
    running = client.get(
        f"/api/v1/agents/{agent_id}/versions/{agent['current_version_id']}", headers=scope
    ).json()
    assert running["spec"]["skills"] == [{"skill_version_id": existing["version"]["id"]}]
    skill = client.get(f"/api/v1/skills/{existing['skill']['id']}", headers=scope).json()
    assert skill["current_version_id"] == existing["version"]["id"]


def test_the_version_it_started_from_is_still_bindable_afterwards(
    client: TestClient, scope: dict[str, str]
) -> None:
    """Rollback after an approval, which is the roadmap's second exit check."""
    existing = _skill(client, scope)
    proposal = _propose(
        client,
        scope,
        [{"path": "SKILL.md", "content": IMPROVED_MD}],
        existing["skill"]["id"],
    )
    approved = client.post(
        f"/api/v1/skill-proposals/{proposal['id']}/approve", headers=scope
    ).json()

    moved = client.put(
        f"/api/v1/skills/{existing['skill']['id']}/current-version",
        headers=scope,
        json={"version_id": approved["id"]},
    )
    assert moved.status_code == 200, moved.text
    rolled = client.put(
        f"/api/v1/skills/{existing['skill']['id']}/current-version",
        headers=scope,
        json={"version_id": existing["version"]["id"]},
    )

    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["current_version_id"] == existing["version"]["id"]


def test_a_proposal_the_scan_blocked_is_readable_and_unapprovable(
    client: TestClient, scope: dict[str, str]
) -> None:
    """§15.3 step 3, both halves, in one request each."""
    proposal = _propose(
        client,
        scope,
        [
            {"path": "SKILL.md", "content": SKILL_MD},
            {"path": "keys.md", "content": A_LEAKED_KEY},
        ],
    )

    assert proposal["approvable"] is False
    assert {item["path"] for item in proposal["findings"]} == {"keys.md"}
    refused = client.post(
        f"/api/v1/skill-proposals/{proposal['id']}/approve", headers=scope
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "skill_proposal_not_approvable"
    assert refused.json()["context"]["findings"][0]["path"] == "keys.md"
    assert client.get("/api/v1/skills", headers=scope).json() == []


def test_a_rejected_proposal_ends_and_creates_nothing(
    client: TestClient, scope: dict[str, str]
) -> None:
    existing = _skill(client, scope)
    proposal = _propose(
        client,
        scope,
        [{"path": "SKILL.md", "content": IMPROVED_MD}],
        existing["skill"]["id"],
    )

    rejected = client.post(
        f"/api/v1/skill-proposals/{proposal['id']}/reject", headers=scope
    )

    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["decided_at"] is not None
    versions = client.get(
        f"/api/v1/skills/{existing['skill']['id']}/versions", headers=scope
    ).json()
    assert len(versions) == 1
    again = client.post(f"/api/v1/skill-proposals/{proposal['id']}/approve", headers=scope)
    assert again.status_code == 409


def test_a_new_skill_can_be_proposed_and_approved_into_existence(
    client: TestClient, scope: dict[str, str]
) -> None:
    proposal = _propose(client, scope, [{"path": "SKILL.md", "content": SKILL_MD}])

    approved = client.post(
        f"/api/v1/skill-proposals/{proposal['id']}/approve", headers=scope
    )

    assert approved.status_code == 201, approved.text
    listed = client.get("/api/v1/skills", headers=scope).json()
    assert [item["name"] for item in listed] == ["rollout"]
    # Its only version is where new bindings start; there is nowhere else the
    # default of a brand new skill could point.
    assert listed[0]["current_version_id"] == approved.json()["id"]


def test_the_queue_can_be_read_by_status(
    client: TestClient, scope: dict[str, str]
) -> None:
    first = _propose(client, scope, [{"path": "SKILL.md", "content": SKILL_MD}])
    _propose(client, scope, [{"path": "SKILL.md", "content": IMPROVED_MD}])
    client.post(f"/api/v1/skill-proposals/{first['id']}/reject", headers=scope)

    pending = client.get(
        "/api/v1/skill-proposals", headers=scope, params={"status": "pending"}
    ).json()
    everything = client.get("/api/v1/skill-proposals", headers=scope).json()

    assert [item["id"] for item in pending] != [first["id"]]
    assert len(pending) == 1
    assert len(everything) == 2


def test_another_workspace_cannot_see_or_decide_this_one_s_proposals(
    client: TestClient, scope: dict[str, str], admin_csrf: str
) -> None:
    proposal = _propose(client, scope, [{"path": "SKILL.md", "content": SKILL_MD}])
    other = client.post(
        "/api/v1/workspaces", headers={"X-CSRF-Token": admin_csrf}, json={"name": "Other"}
    )
    assert other.status_code == 201
    elsewhere = {"X-Workspace-Id": other.json()["id"], "X-CSRF-Token": admin_csrf}

    assert client.get("/api/v1/skill-proposals", headers=elsewhere).json() == []
    read = client.get(f"/api/v1/skill-proposals/{proposal['id']}", headers=elsewhere)
    approve = client.post(
        f"/api/v1/skill-proposals/{proposal['id']}/approve", headers=elsewhere
    )

    assert read.status_code == 404
    assert approve.status_code == 404


async def test_every_decision_is_written_to_the_audit_log(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine
) -> None:
    approved = _propose(client, scope, [{"path": "SKILL.md", "content": SKILL_MD}])
    rejected = _propose(client, scope, [{"path": "SKILL.md", "content": IMPROVED_MD}])
    client.post(f"/api/v1/skill-proposals/{approved['id']}/approve", headers=scope)
    client.post(f"/api/v1/skill-proposals/{rejected['id']}/reject", headers=scope)

    async with engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT action FROM audit_events ORDER BY created_at")
        )
        written = [str(row[0]) for row in rows.all()]

    assert "skill.proposal_opened" in written
    assert "skill.proposal_approved" in written
    assert "skill.proposal_rejected" in written
