"""Publishing against a real endpoint, and the four ways it is refused.

Checked at publish rather than at draft: a draft is a work in progress and an
author is allowed to save one that is not ready. A version is immutable and a Run
will execute it, so it is the last moment a mistake is still cheap.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from ..conftest import VALID_SPEC

CREDENTIAL = "TINY_HERMES_TEST_MODEL_KEY"

ENDPOINT: dict[str, Any] = {
    "name": "acme-gpt",
    "kind": "openai_compatible",
    "base_url": "https://models.example.com/v1",
    "model": "acme-large",
    "context_window": 128_000,
    "max_output_tokens": 4_096,
    "usage_quality": "provider",
    "credential_ref": CREDENTIAL,
}


@pytest.fixture(autouse=True)
def credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CREDENTIAL, "not-a-real-key")


@pytest.fixture
def endpoint_id(client: TestClient, admin_csrf: str) -> str:
    created = client.post(
        "/api/v1/model-endpoints", headers={"X-CSRF-Token": admin_csrf}, json=ENDPOINT
    )
    assert created.status_code == 201
    return str(created.json()["id"])


@pytest.fixture
def smaller_endpoint_id(client: TestClient, admin_csrf: str) -> str:
    """A second endpoint with a genuinely smaller window than `ENDPOINT`'s
    128,000 — for the `summary_endpoint_id` refusal below, which is about two
    whole context windows and has nothing to do with the segment budget the
    rest of this file's endpoint is sized for."""
    created = client.post(
        "/api/v1/model-endpoints",
        headers={"X-CSRF-Token": admin_csrf},
        json={
            **ENDPOINT,
            "name": "acme-summary-small",
            "context_window": 32_000,
            "max_output_tokens": 4_096,
        },
    )
    assert created.status_code == 201
    return str(created.json()["id"])


def _agent(client: TestClient, scope: dict[str, str], alias: str = "analyst") -> str:
    created = client.post(
        "/api/v1/agents", headers=scope, json={"name": alias.title(), "alias": alias}
    )
    assert created.status_code == 201
    return str(created.json()["id"])


def _save(
    client: TestClient, scope: dict[str, str], agent_id: str, **policy: Any
) -> Any:
    return client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={
            "expected_revision": 1,
            "spec": {**VALID_SPEC, "model_policy": policy},
        },
    )


def _publish(client: TestClient, scope: dict[str, str], agent_id: str) -> Any:
    return client.post(
        f"/api/v1/agents/{agent_id}/publish", headers=scope, json={"expected_revision": 2}
    )


def test_an_agent_publishes_against_a_registered_endpoint(
    client: TestClient, scope: dict[str, str], endpoint_id: str
) -> None:
    agent_id = _agent(client, scope)
    assert _save(
        client, scope, agent_id, provider="openai_compatible", endpoint_id=endpoint_id
    ).status_code == 200
    published = _publish(client, scope, agent_id)
    assert published.status_code == 201
    assert published.json()["version_number"] == 1


def test_a_draft_may_name_an_endpoint_that_does_not_exist(
    client: TestClient, scope: dict[str, str]
) -> None:
    """A draft is a work in progress, and refusing to save one is how a person
    loses what they typed."""
    agent_id = _agent(client, scope)
    saved = _save(
        client,
        scope,
        agent_id,
        provider="openai_compatible",
        endpoint_id="00000000-0000-4000-8000-000000000000",
    )
    assert saved.status_code == 200


def test_publishing_against_an_endpoint_that_does_not_exist_is_refused(
    client: TestClient, scope: dict[str, str]
) -> None:
    agent_id = _agent(client, scope)
    _save(
        client,
        scope,
        agent_id,
        provider="openai_compatible",
        endpoint_id="00000000-0000-4000-8000-000000000000",
    )
    refused = _publish(client, scope, agent_id)
    assert refused.status_code == 422
    assert refused.json()["code"] == "model_endpoint_unavailable"


def test_publishing_against_a_disabled_endpoint_is_refused(
    client: TestClient, scope: dict[str, str], endpoint_id: str, admin_csrf: str
) -> None:
    agent_id = _agent(client, scope)
    _save(client, scope, agent_id, provider="openai_compatible", endpoint_id=endpoint_id)
    client.patch(
        f"/api/v1/model-endpoints/{endpoint_id}",
        headers={"X-CSRF-Token": admin_csrf},
        json={"status": "disabled"},
    )
    refused = _publish(client, scope, agent_id)
    assert refused.status_code == 422
    assert refused.json()["code"] == "model_endpoint_unavailable"


def test_an_output_limit_above_the_endpoints_own_is_refused_not_clamped(
    client: TestClient, scope: dict[str, str], endpoint_id: str
) -> None:
    """A limit that quietly becomes a different limit is worse than an error.

    An author who asked for 8192 and got 4096 has an Agent that behaves unlike
    the one they published, and nothing anywhere says so.
    """
    agent_id = _agent(client, scope)
    _save(
        client,
        scope,
        agent_id,
        provider="openai_compatible",
        endpoint_id=endpoint_id,
        max_output_tokens=8_192,
    )
    refused = _publish(client, scope, agent_id)
    assert refused.status_code == 422
    assert refused.json()["code"] == "model_output_limit_too_high"


def test_an_output_limit_within_the_endpoints_own_is_allowed(
    client: TestClient, scope: dict[str, str], endpoint_id: str
) -> None:
    agent_id = _agent(client, scope)
    _save(
        client,
        scope,
        agent_id,
        provider="openai_compatible",
        endpoint_id=endpoint_id,
        max_output_tokens=1_024,
    )
    assert _publish(client, scope, agent_id).status_code == 201


def test_a_deterministic_agent_publishes_with_no_endpoint_involved(
    client: TestClient, scope: dict[str, str]
) -> None:
    """The stand-in stays selectable, and its path is untouched by any of this."""
    agent_id = _agent(client, scope)
    assert _save(
        client, scope, agent_id, provider="deterministic", scenario="complete"
    ).status_code == 200
    assert _publish(client, scope, agent_id).status_code == 201


def test_no_token_limit_can_be_configured_at_all(
    client: TestClient, scope: dict[str, str]
) -> None:
    """Why technical design §9.4's rule is not implemented in this slice.

    §9.4 says an AgentVersion demanding a strict Token ceiling cannot select an
    endpoint whose usage is unavailable. `AgentLimits` has no Token field, and
    `run_budget_scopes.max_tokens` is written as NULL for every Run, so there is
    no such AgentVersion to refuse — the rule would be a branch nothing can
    reach.

    Adding the field is not free: every published spec is validated on read
    against `schema_version: Literal[1]`, and a new field changes the normalized
    document and therefore the content hash, so the Token limit and the ability
    to read more than one schema version have to arrive together. That is a
    slice of its own. This test fails the moment somebody adds the field, which
    is exactly when the §9.4 rule has to be written.
    """
    agent_id = _agent(client, scope)
    refused = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={
            "expected_revision": 1,
            "spec": {
                **VALID_SPEC,
                "limits": {**VALID_SPEC["limits"], "max_tokens": 100_000},
            },
        },
    )
    assert refused.status_code == 422


def test_an_unavailable_usage_endpoint_publishes_and_is_recorded_as_such(
    client: TestClient, scope: dict[str, str], admin_csrf: str
) -> None:
    """It is selectable, and what it costs the platform is visibility, not safety.

    Time and model-call limits are enforced unchanged; only Token accounting is
    blind, and the Run says so through `checkpoint_usage_quality`.
    """
    created = client.post(
        "/api/v1/model-endpoints",
        headers={"X-CSRF-Token": admin_csrf},
        json={**ENDPOINT, "name": "silent-gpt", "usage_quality": "unavailable"},
    )
    silent = str(created.json()["id"])
    agent_id = _agent(client, scope)
    _save(client, scope, agent_id, provider="openai_compatible", endpoint_id=silent)
    assert _publish(client, scope, agent_id).status_code == 201


def test_a_smaller_summary_endpoint_is_refused_through_the_route(
    client: TestClient, scope: dict[str, str], endpoint_id: str, smaller_endpoint_id: str
) -> None:
    """Task 4's domain test (`tests/unit/agents/test_summary_endpoint.py`)
    pins the exception's own `str()`. That is not what a developer reads:
    `routes.py` is the only place that turns `ContextBudgetUnsatisfied` into
    the HTTP `detail` a caller actually sees, and it used to rebuild that
    detail from `error.fit` — which this refusal never set — producing a
    segment-budget sentence naming no summary endpoint and dangling a
    "Suggested targets: " with nothing after it. This asserts the response
    body itself, through the real `/publish` route.
    """
    agent_id = _agent(client, scope)
    _save(
        client,
        scope,
        agent_id,
        provider="openai_compatible",
        endpoint_id=endpoint_id,
        summary_endpoint_id=smaller_endpoint_id,
    )
    refused = _publish(client, scope, agent_id)
    assert refused.status_code == 422
    body = refused.json()
    assert body["code"] == "context_budget_unsatisfied"
    detail = body["detail"]
    # Names which endpoint is the problem, and both of its numbers — not the
    # segment sentence built from a `BudgetFit` this refusal never had.
    assert smaller_endpoint_id in detail
    assert "32000" in detail
    assert "128000" in detail
    assert "suggested targets" not in detail.lower()
