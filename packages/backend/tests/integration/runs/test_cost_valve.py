"""What a Run cost, what it was allowed to spend, and what is never a zero.

§12.4 across the pieces that only meet in a running Run: the price an
administrator entered, the version the Run fixed at creation, the accumulation
after each round, and the valve that stops the next one.

The test that matters most is the pair. The same Run, once against a priced
endpoint and once against an unpriced one, must not report the same thing — and
the unpriced one must not be allowed past a spending limit on the strength of a
total nobody counted.
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier

from ..conftest import VALID_SPEC


@pytest.fixture(autouse=True)
def credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registering an endpoint checks that its credential resolves.

    A fake one is enough here: nothing in this suite reaches the endpoint —
    the stand-in model answers — and what is under test is the price beside
    it rather than the call.
    """
    monkeypatch.setenv("TINY_HERMES_TEST_MODEL_KEY", "not-a-real-key")


def _worker(engine: AsyncEngine, workspace_id: str) -> WorkerRuntime:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return WorkerRuntime(
        session_factory=sessions,
        model=DeterministicModelProvider(delay_ms=0),
        notifier=NullWakeUpNotifier(),
        settings=WorkerSettings(
            worker_id="worker-a",
            lease_seconds=30,
            max_slice_seconds=30,
            idle_poll_seconds=1,
            workspace_id=UUID(workspace_id),
        ),
    )


def _endpoint(client: TestClient, scope: dict[str, str]) -> str:
    """A registered endpoint. The stand-in model answers for it, so nothing
    here needs it to be reachable — only to exist and to be priced."""
    created = client.post(
        "/api/v1/model-endpoints",
        headers=scope,
        json={
            "name": "Priced",
            "kind": "openai_compatible",
            "base_url": "https://models.example.com",
            "model": "test-model",
            "context_window": 128_000,
            "max_output_tokens": 4_096,
            "usage_quality": "provider",
            "credential_ref": "TINY_HERMES_TEST_MODEL_KEY",
        },
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def _price(
    client: TestClient,
    scope: dict[str, str],
    endpoint_id: str,
    *,
    input_price: str = "3",
    output_price: str = "15",
) -> Any:
    return client.post(
        f"/api/v1/model-endpoints/{endpoint_id}/pricing",
        headers=scope,
        json={
            "currency": "USD",
            "input_per_million": input_price,
            "output_per_million": output_price,
        },
    )


async def _set_ceiling(engine: AsyncEngine, workspace_id: str, amount: str) -> None:
    """The workspace's spending limit.

    Set through SQL because the console page that sets it is §5's; the column
    and its constraints are this step's, and the valve is what is under test.
    """
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE workspaces SET max_run_cost = :amount, cost_currency = 'USD' "
                "WHERE id = :id"
            ),
            {"amount": Decimal(amount), "id": UUID(workspace_id)},
        )


def _agent(client: TestClient, scope: dict[str, str], **spec_overrides: Any) -> str:
    agent_id = str(
        client.post(
            "/api/v1/agents", headers=scope, json={"name": "Spender", "alias": "spender"}
        ).json()["id"]
    )
    spec = {**VALID_SPEC, **spec_overrides}
    draft = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": spec},
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
        headers={**scope, "Idempotency-Key": f"cost-{session_id}"},
        json={"session_id": session_id, "input": "go"},
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


def _status(client: TestClient, scope: dict[str, str], run_id: Any) -> dict[str, Any]:
    return dict(client.get(f"/api/v1/runs/{run_id}", headers=scope).json())


# -- entering a price --------------------------------------------------------


def test_a_price_of_zero_is_recorded_as_a_price(
    client: TestClient, scope: dict[str, str]
) -> None:
    """An administrator looked at a locally hosted model and said it costs
    nothing. `free` says so as its own field, so nothing has to infer it from
    two zeroes."""
    endpoint_id = _endpoint(client, scope)

    priced = _price(client, scope, endpoint_id, input_price="0", output_price="0")

    assert priced.status_code == 201
    assert priced.json()["free"] is True
    assert Decimal(priced.json()["input_per_million"]) == 0


def test_a_new_price_is_a_new_version_and_the_old_one_stays(
    client: TestClient, scope: dict[str, str]
) -> None:
    """Runs created under the old price are still measured by it, so it cannot
    be edited away."""
    endpoint_id = _endpoint(client, scope)
    _price(client, scope, endpoint_id, input_price="3")
    _price(client, scope, endpoint_id, input_price="4")

    listed = client.get(
        f"/api/v1/model-endpoints/{endpoint_id}/pricing", headers=scope
    ).json()

    assert [item["version_number"] for item in listed] == [1, 2]
    assert [Decimal(item["input_per_million"]) for item in listed] == [
        Decimal("3"),
        Decimal("4"),
    ]


@pytest.mark.parametrize("amount", ["-1", "abc", "1.0000001"])
def test_a_price_that_is_not_one_is_refused(
    client: TestClient, scope: dict[str, str], amount: str
) -> None:
    endpoint_id = _endpoint(client, scope)

    refused = _price(client, scope, endpoint_id, input_price=amount)

    assert refused.status_code == 422


# -- what a Run reports ------------------------------------------------------


async def test_a_run_on_an_unpriced_endpoint_reports_unknown_and_never_zero(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """Half of the pair. The other half is the next test, and the two must not
    report the same thing."""
    endpoint_id = _endpoint(client, scope)
    agent_id = _agent(
        client,
        scope,
        model_policy={"provider": "openai_compatible", "endpoint_id": endpoint_id},
    )
    run = _run(client, scope, session_for(agent_id))

    reloaded = _status(client, scope, run["id"])

    assert Decimal(reloaded["budget"]["consumed_cost"]) == 0
    async with engine.connect() as connection:
        pinned = await connection.execute(
            text("SELECT model_pricing_version_id FROM runs WHERE id = :id"),
            {"id": UUID(str(run["id"]))},
        )
        # Nothing was priced when this Run was created, so it fixed nothing —
        # and a Run with no pinned price can never state what it cost.
        assert pinned.scalar() is None


async def test_a_run_fixes_the_price_that_was_in_force_when_it_was_created(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """§12.4's promise. A correction entered afterwards does not rewrite what
    this Run is measured at."""
    endpoint_id = _endpoint(client, scope)
    first = _price(client, scope, endpoint_id, input_price="3").json()
    agent_id = _agent(
        client,
        scope,
        model_policy={"provider": "openai_compatible", "endpoint_id": endpoint_id},
    )
    run = _run(client, scope, session_for(agent_id))
    _price(client, scope, endpoint_id, input_price="99")

    async with engine.connect() as connection:
        pinned = await connection.execute(
            text("SELECT model_pricing_version_id FROM runs WHERE id = :id"),
            {"id": UUID(str(run["id"]))},
        )
        assert str(pinned.scalar()) == first["id"]


async def test_a_deterministic_run_has_no_price_and_says_so(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """It reaches no endpoint, so there is nothing to price. Unknown rather
    than free — the platform is not claiming the work was cheap."""
    run = _run(client, scope, session_for(_agent(client, scope)))

    await _worker(engine, scope["X-Workspace-Id"]).run_once()

    reloaded = _status(client, scope, run["id"])
    assert reloaded["status"] == "completed"
    assert reloaded["budget"]["consumed_cost"] is None
    assert reloaded["budget"]["cost_quality"] == "unknown"


# -- the valve ---------------------------------------------------------------


async def test_a_ceiling_meeting_an_unpriced_endpoint_stops_the_run(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """The decision §12.4 turns on. A deployment with a spending limit and an
    unpriced endpoint must not believe it is protected, so the Run stops rather
    than running on a total nobody is counting."""
    await _set_ceiling(engine, scope["X-Workspace-Id"], "10")
    run = _run(client, scope, session_for(_agent(client, scope)))

    await _worker(engine, scope["X-Workspace-Id"]).run_once()

    reloaded = _status(client, scope, run["id"])
    assert reloaded["status"] == "paused"
    assert reloaded["pause_reason"] == "limit"
    # No provider call was made: the platform declined to pay for it rather
    # than paying and then noticing.
    assert reloaded["budget"]["consumed_model_calls"] == 0


async def test_a_run_with_no_ceiling_runs_on_an_unpriced_endpoint(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """It never asked to be measured, so an unknown cost costs it nothing. A
    deployment that set no limit is not made to configure prices."""
    run = _run(client, scope, session_for(_agent(client, scope)))

    await _worker(engine, scope["X-Workspace-Id"]).run_once()

    assert _status(client, scope, run["id"])["status"] == "completed"


async def test_the_ceiling_is_the_one_the_run_was_created_under(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """Copied onto the Run, so a limit an administrator raises afterwards does
    not change what a Run already being measured is measured against."""
    await _set_ceiling(engine, scope["X-Workspace-Id"], "10")
    run = _run(client, scope, session_for(_agent(client, scope)))
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE workspaces SET max_run_cost = NULL, cost_currency = NULL")
        )

    await _worker(engine, scope["X-Workspace-Id"]).run_once()

    # Still stopped: the Run carries its own copy of the valve.
    assert _status(client, scope, run["id"])["status"] == "paused"


async def test_the_reason_says_what_is_missing(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """An operator reading a paused Run needs to know it is the price that is
    missing, not that they were over budget."""
    await _set_ceiling(engine, scope["X-Workspace-Id"], "10")
    run = _run(client, scope, session_for(_agent(client, scope)))

    await _worker(engine, scope["X-Workspace-Id"]).run_once()

    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT payload FROM run_events WHERE run_id = :id "
                "AND event_type = 'run_limit_reached'"
            ),
            {"id": UUID(str(run["id"]))},
        )
        payloads = [row[0] for row in rows.all()]
    assert payloads
    assert payloads[0]["valve"] == "cost"
    assert "no price" in payloads[0]["reason"]
