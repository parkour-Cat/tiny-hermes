"""§6's usage half over the real HTTP boundary and the real database.

An administrator asking "how much has this workspace spent" needs a number
they can trust the shape of before they trust its size. These tests pin two
things `GET /api/v1/usage` must do: group `consumed_cost` by `cost_quality`
so a provider figure and an unknown one are never summed into one, and never
leak another workspace's spend into this one's total.
"""

from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .test_cost_valve import _agent, _endpoint, _price, _run, _status, _worker


@pytest.fixture(autouse=True)
def credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The priced endpoint's registration checks that its credential resolves.

    A fake one is enough: the stand-in model answers, so nothing here reaches
    the endpoint for real — only the price beside it is under test.
    """
    monkeypatch.setenv("TINY_HERMES_TEST_MODEL_KEY", "not-a-real-key")


def _usage(client: TestClient, scope: dict[str, str]) -> dict[str, Any]:
    response = client.get("/api/v1/usage", headers=scope)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _by_quality(usage: dict[str, Any], quality: str) -> dict[str, Any] | None:
    for bucket in usage["by_cost_quality"]:
        if bucket["cost_quality"] == quality:
            return dict(bucket)
    return None


async def test_provider_and_unknown_cost_never_share_a_bucket(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Any,
) -> None:
    """The core requirement: a real invoice and "nobody knows" must arrive as
    two separate totals, not folded into one number a reader could mistake
    for either."""
    priced_endpoint = _endpoint(client, scope)
    _price(client, scope, priced_endpoint, input_price="3", output_price="15")
    priced_agent = _agent(
        client,
        scope,
        model_policy={"provider": "openai_compatible", "endpoint_id": priced_endpoint},
    )
    priced_run = _run(client, scope, session_for(priced_agent))
    unpriced_run = _run(client, scope, session_for(_agent(client, scope)))

    await _worker(engine, scope["X-Workspace-Id"]).run_once()
    await _worker(engine, scope["X-Workspace-Id"]).run_once()

    assert _status(client, scope, priced_run["id"])["status"] == "completed"
    assert _status(client, scope, unpriced_run["id"])["status"] == "completed"

    usage = _usage(client, scope)

    assert usage["window"] == "all_time"
    provider = _by_quality(usage, "provider")
    unknown = _by_quality(usage, "unknown")
    assert provider is not None
    assert unknown is not None
    # A real figure, never null, and never merged with the unpriced Run's.
    assert Decimal(provider["consumed_cost"]) > 0
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
    buckets. Stamped directly onto the row, the same way the cost valve
    suite sets a ceiling straight through SQL."""
    run = _run(client, scope, session_for(_agent(client, scope)))
    await _worker(engine, scope["X-Workspace-Id"]).run_once()
    assert _status(client, scope, run["id"])["status"] == "completed"

    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE run_budget_scopes SET cost_quality = 'estimated', "
                "consumed_cost = 7.5, cost_currency = 'USD' "
                "WHERE root_run_id = :id"
            ),
            {"id": UUID(str(run["id"]))},
        )

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
    endpoint_id = _endpoint(client, scope)
    _price(client, scope, endpoint_id, input_price="3")
    agent_id = _agent(
        client,
        scope,
        model_policy={"provider": "openai_compatible", "endpoint_id": endpoint_id},
    )
    _run(client, scope, session_for(agent_id))
    await _worker(engine, scope["X-Workspace-Id"]).run_once()

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
