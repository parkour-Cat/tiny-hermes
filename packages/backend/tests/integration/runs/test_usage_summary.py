"""§6's usage half over the real HTTP boundary and the real database.

An administrator asking "how much has this workspace spent" needs a number
they can trust the shape of before they trust its size. These tests pin two
things `GET /api/v1/usage` must do: group `consumed_cost` by `cost_quality`
so a provider figure and an unknown one are never summed into one, and never
leak another workspace's spend into this one's total.

Budget rows are stamped directly through SQL rather than produced by running
a Run against a priced endpoint — the same technique `test_cost_valve.py`
uses for its spending ceiling. What is under test here is the aggregation
query grouping `run_budget_scopes` by `cost_quality`, not the pipeline that
fills those columns in production; that pipeline already has its own
coverage in `test_cost_valve.py` and `test_openai_provider.py`.
"""

from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..conftest import VALID_SPEC


def _agent(client: TestClient, scope: dict[str, str]) -> str:
    """Not imported from `test_cost_valve`: its helper of the same name is
    module-private, and what this suite needs from it is only "a published
    Agent", with none of the pricing it exists to set up."""
    agent_id = str(
        client.post(
            "/api/v1/agents", headers=scope, json={"name": "Spender", "alias": "spender"}
        ).json()["id"]
    )
    draft = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": VALID_SPEC},
    )
    assert draft.status_code == 200, draft.text
    published = client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=scope,
        json={"expected_revision": draft.json()["revision"]},
    )
    assert published.status_code == 201, published.text
    return agent_id


def _run(client: TestClient, scope: dict[str, str], session_id: str) -> dict[str, Any]:
    created = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": f"usage-{session_id}"},
        json={"session_id": session_id, "input": "go"},
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


def _usage(client: TestClient, scope: dict[str, str]) -> dict[str, Any]:
    response = client.get("/api/v1/usage", headers=scope)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _by_quality(usage: dict[str, Any], quality: str) -> dict[str, Any] | None:
    for bucket in usage["by_cost_quality"]:
        if bucket["cost_quality"] == quality:
            return dict(bucket)
    return None


async def _stamp_budget(
    engine: AsyncEngine,
    run_id: Any,
    *,
    quality: str,
    cost: str | None,
    currency: str | None,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE run_budget_scopes SET cost_quality = :quality, "
                "consumed_cost = :cost, cost_currency = :currency, "
                "consumed_model_calls = 3, consumed_tool_calls = 1, "
                "consumed_tokens = 500, consumed_execution_ms = 1000 "
                "WHERE root_run_id = :id"
            ),
            {
                "quality": quality,
                "cost": None if cost is None else Decimal(cost),
                "currency": currency,
                "id": UUID(str(run_id)),
            },
        )


async def test_provider_and_unknown_cost_never_share_a_bucket(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Any,
) -> None:
    """The core requirement: a real invoice and "nobody knows" must arrive as
    two separate totals, not folded into one number a reader could mistake
    for either."""
    agent_id = _agent(client, scope)
    priced_run = _run(client, scope, session_for(agent_id))
    unpriced_run = _run(client, scope, session_for(agent_id))
    await _stamp_budget(
        engine, priced_run["id"], quality="provider", cost="12.34", currency="USD"
    )
    # A freshly created budget scope starts `provider`/`0` — a genuine "spent
    # nothing yet" (`_new_budget`'s own comment) — and only turns `unknown`
    # once a round completes without a price. Stamped explicitly here rather
    # than left at Run-creation's default, so this test is about the grouping
    # query and not about that transition.
    await _stamp_budget(engine, unpriced_run["id"], quality="unknown", cost=None, currency=None)

    usage = _usage(client, scope)

    assert usage["window"] == "all_time"
    provider = _by_quality(usage, "provider")
    unknown = _by_quality(usage, "unknown")
    assert provider is not None
    assert unknown is not None
    assert Decimal(provider["consumed_cost"]) == Decimal("12.34")
    assert provider["cost_currency"] == "USD"
    assert provider["run_count"] == 1
    # Unpriced stays unknown, not zero — the same rule §12.4 already applies
    # per-Run, carried up to the workspace rollup.
    assert unknown["consumed_cost"] is None
    assert unknown["cost_currency"] is None
    assert unknown["run_count"] == 1
    # No key anywhere outside a bucket can hold a cost figure: that is what
    # keeps a caller from reaching a blended number by accident.
    cost_like_top_level_keys = {
        key for key in usage if "cost" in key and key != "by_cost_quality"
    }
    assert cost_like_top_level_keys == set()
    assert usage["total_run_count"] == 2


async def test_usage_generalizes_to_a_third_quality_bucket(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Any,
) -> None:
    """`estimated` has no production path yet (`CostQuality.ESTIMATED`'s own
    docstring), but the grouping must not have been written for exactly two
    buckets."""
    run = _run(client, scope, session_for(_agent(client, scope)))
    await _stamp_budget(engine, run["id"], quality="estimated", cost="7.5", currency="USD")

    usage = _usage(client, scope)

    estimated = _by_quality(usage, "estimated")
    assert estimated is not None
    assert Decimal(estimated["consumed_cost"]) == Decimal("7.5")
    assert estimated["run_count"] == 1


async def test_usage_does_not_leak_across_workspaces(
    client: TestClient,
    scope: dict[str, str],
    admin_csrf: str,
    engine: AsyncEngine,
    session_for: Any,
) -> None:
    """A read that is supposed to be scoped to one workspace must not let
    another workspace's spend inflate this one's total — the same boundary
    `list_runs` already holds."""
    run = _run(client, scope, session_for(_agent(client, scope)))
    await _stamp_budget(engine, run["id"], quality="provider", cost="9", currency="USD")

    other_workspace = client.post(
        "/api/v1/workspaces",
        headers={"X-CSRF-Token": admin_csrf},
        json={"name": "Other"},
    )
    assert other_workspace.status_code == 201
    other_scope = {
        "X-Workspace-Id": str(other_workspace.json()["id"]),
        "X-CSRF-Token": admin_csrf,
    }

    usage = _usage(client, other_scope)

    assert usage["by_cost_quality"] == []
    assert usage["total_run_count"] == 0


def test_an_unauthenticated_caller_is_refused(client: TestClient, scope: dict[str, str]) -> None:
    """No session, no answer — the same gate `list_runs` sits behind."""
    client.cookies.clear()

    response = client.get(
        "/api/v1/usage", headers={"X-Workspace-Id": scope["X-Workspace-Id"]}
    )

    assert response.status_code == 401
