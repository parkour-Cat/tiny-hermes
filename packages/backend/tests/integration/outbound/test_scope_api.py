"""The two levels over HTTP, against the database, and what an Agent may ask.

The unit tests say what the rules decide. These say the rules, the routes, the
table and the Agent catalog agree — and the one that matters most is the last
test in the file: registering a model endpoint approves its host, and disabling
the endpoint takes the approval away, so the two can never disagree about what
this platform may reach.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..conftest import VALID_SPEC


@pytest.fixture(autouse=True)
def credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployment's side of the bargain: the named variable exists.

    A value that looks nothing like a key, so nothing here could be mistaken
    for one if it ever turned up somewhere it should not.
    """
    monkeypatch.setenv("TINY_HERMES_TEST_MODEL_KEY", "not-a-real-key")


ENDPOINT: dict[str, Any] = {
    "name": "acme-gpt",
    "kind": "openai_compatible",
    "base_url": "https://models.example.com/v1",
    "model": "acme-large",
    "context_window": 128_000,
    "max_output_tokens": 4_096,
    "usage_quality": "provider",
    "credential_ref": "TINY_HERMES_TEST_MODEL_KEY",
}


def approve_platform(client: TestClient, csrf: str, entry: str) -> dict[str, Any]:
    created = client.post(
        "/api/v1/outbound-scopes/platform",
        headers={"X-CSRF-Token": csrf},
        json={"entry": entry},
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


def approve_workspace(
    client: TestClient, scope: dict[str, str], entry: str
) -> Any:
    return client.post(
        "/api/v1/outbound-scopes/workspace", headers=scope, json={"entry": entry}
    )


def test_a_platform_entry_is_visible_to_a_workspace_choosing_inside_it(
    client: TestClient, scope: dict[str, str], admin_csrf: str
) -> None:
    approve_platform(client, admin_csrf, "*.example.com")

    listed = client.get("/api/v1/outbound-scopes/platform", headers=scope)

    assert listed.status_code == 200
    assert [entry["entry"] for entry in listed.json()] == ["*.example.com"]


def test_a_workspace_may_choose_inside_and_not_outside(
    client: TestClient, scope: dict[str, str], admin_csrf: str
) -> None:
    approve_platform(client, admin_csrf, "*.example.com")

    inside = approve_workspace(client, scope, "api.example.com")
    outside = approve_workspace(client, scope, "payments.other.example")

    assert inside.status_code == 201, inside.text
    assert outside.status_code == 422
    assert outside.json()["code"] == "outbound_entry_outside_platform"
    listed = client.get("/api/v1/outbound-scopes/workspace", headers=scope).json()
    assert [entry["entry"] for entry in listed] == ["api.example.com"]


def test_an_entry_nobody_could_review_is_refused(
    client: TestClient, admin_csrf: str
) -> None:
    refused = client.post(
        "/api/v1/outbound-scopes/platform",
        headers={"X-CSRF-Token": admin_csrf},
        json={"entry": "*.com"},
    )

    assert refused.status_code == 422
    assert refused.json()["code"] == "invalid_outbound_entry"


def test_approving_the_same_entry_twice_is_the_same_entry(
    client: TestClient, admin_csrf: str
) -> None:
    """An administrator who clicked twice meant the same thing both times."""
    first = approve_platform(client, admin_csrf, "*.example.com")
    second = approve_platform(client, admin_csrf, "*.example.com")

    assert first["id"] == second["id"]


def test_revoking_takes_the_target_away(
    client: TestClient, scope: dict[str, str], admin_csrf: str
) -> None:
    approve_platform(client, admin_csrf, "*.example.com")
    entry = approve_workspace(client, scope, "api.example.com").json()

    removed = client.delete(f"/api/v1/outbound-scopes/{entry['id']}", headers=scope)

    assert removed.status_code == 204
    assert client.get("/api/v1/outbound-scopes/workspace", headers=scope).json() == []


# -- what an Agent may ask for ----------------------------------------------


def publish_with_network(
    client: TestClient, scope: dict[str, str], allow: list[str], alias: str = "runner"
) -> Any:
    agent_id = str(
        client.post(
            "/api/v1/agents", headers=scope, json={"name": alias.title(), "alias": alias}
        ).json()["id"]
    )
    draft = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": {**VALID_SPEC, "network": {"allow": allow}}},
    )
    assert draft.status_code == 200, draft.text
    return client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=scope,
        json={"expected_revision": draft.json()["revision"]},
    )


def test_an_agent_may_name_a_target_its_workspace_approved(
    client: TestClient, scope: dict[str, str], admin_csrf: str
) -> None:
    approve_platform(client, admin_csrf, "*.example.com")
    approve_workspace(client, scope, "api.example.com")

    published = publish_with_network(client, scope, ["api.example.com"])

    assert published.status_code == 201, published.text


def test_an_agent_naming_something_the_workspace_did_not_approve_is_refused(
    client: TestClient, scope: dict[str, str], admin_csrf: str
) -> None:
    """A layer may narrow and never widen, and the refusal names every entry
    that broke the rule."""
    approve_platform(client, admin_csrf, "*.example.com")
    approve_workspace(client, scope, "api.example.com")

    refused = publish_with_network(
        client, scope, ["api.example.com", "docs.example.com"]
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "agent_network_outside_workspace"
    assert refused.json()["context"]["entries"] == ["docs.example.com"]


# -- the endpoint that approves its own host --------------------------------


async def test_registering_an_endpoint_approves_its_host_and_disabling_takes_it_back(
    client: TestClient, admin_csrf: str, engine: AsyncEngine
) -> None:
    """Choosing an endpoint *is* the approval, and it is not two facts.

    An administrator who had to write the host into the outbound scope as well
    would forget, and the symptom of forgetting is a Run failing at runtime a
    long way from the cause.
    """
    registered = client.post(
        "/api/v1/model-endpoints", headers={"X-CSRF-Token": admin_csrf}, json=ENDPOINT
    )
    assert registered.status_code == 201, registered.text
    endpoint_id = registered.json()["id"]

    listed = client.get(
        "/api/v1/outbound-scopes/platform", headers={"X-CSRF-Token": admin_csrf}
    ).json()
    approved = [entry for entry in listed if entry["entry"] == "models.example.com"]
    assert len(approved) == 1
    # Owned by the endpoint, so nobody edits it by hand: removing it would make
    # the endpoint unreachable with nothing saying why.
    assert approved[0]["managed"] is True
    refused = client.delete(
        f"/api/v1/outbound-scopes/{approved[0]['id']}",
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "outbound_entry_managed"

    disabled = client.patch(
        f"/api/v1/model-endpoints/{endpoint_id}",
        headers={"X-CSRF-Token": admin_csrf},
        json={"status": "disabled"},
    )
    assert disabled.status_code == 200, disabled.text

    async with engine.connect() as connection:
        remaining = await connection.execute(
            text("SELECT count(*) FROM outbound_scopes WHERE entry = 'models.example.com'")
        )
        assert remaining.scalar_one() == 0


async def test_naming_an_unapproved_target_is_refused_and_recorded(
    client: TestClient,
    scope: dict[str, str],
    admin_csrf: str,
    engine: AsyncEngine,
) -> None:
    """§23 assertion 14's second half, which was never built.

    "配置或连接被拒绝**并写安全审计事件**". The refusal is covered by the test
    above; the event had no implementation — `AgentNetworkOutsideWorkspace`
    maps to a 422 with `audited` left at its default, so nothing survived
    the rolled-back transaction.

    This is the same gap assertion 2 had, in a different module, and it
    matters for the same reason: an Agent repeatedly published against
    targets its workspace never approved is a thing somebody should be able
    to notice afterwards. Without the row there is nothing to notice — the
    author sees a 422 and the workspace sees nothing at all.
    """
    approve_platform(client, admin_csrf, "*.example.com")
    approve_workspace(client, scope, "api.example.com")

    refused = publish_with_network(client, scope, ["api.example.com", "evil.example.com"])

    assert refused.status_code == 422, refused.text
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT actor_type, result, context FROM audit_events"
                " WHERE action = 'agent.network_refused'"
            )
        )
        events = list(rows.all())

    assert len(events) == 1
    assert events[0].result == "denied"
    # The entries that broke the rule, so the row answers "what did they
    # reach for" and not only "somebody was refused".
    assert events[0].context["entries"] == ["evil.example.com"]
