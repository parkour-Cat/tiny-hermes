"""Per-workspace run limits (product design §1.21.1 and .3, v2.21).

Design: docs/superpowers/specs/2026-10-23-workspace-limits-design.md.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..conftest import VALID_SPEC


def _limits(client: TestClient, workspace_id: str, headers: dict[str, str]) -> Any:
    return client.get(f"/api/v1/workspaces/{workspace_id}/limits", headers=headers)


def _set(client: TestClient, workspace_id: str, headers: dict[str, str], **values: Any) -> Any:
    return client.put(f"/api/v1/workspaces/{workspace_id}/limits", headers=headers, json=values)


def _draft(client: TestClient, scope: dict[str, str], **limits: int) -> Any:
    agent = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Long", "alias": f"long-{uuid4().hex[:8]}"}
    ).json()
    spec = {**VALID_SPEC, "limits": {**VALID_SPEC.get("limits", {}), **limits}}
    return client.put(
        f"/api/v1/agents/{agent['id']}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": spec},
    )


def test_unset_limits_read_as_the_platform_defaults(
    client: TestClient, workspace_id: str, scope: dict[str, str]
) -> None:
    read = _limits(client, workspace_id, scope)

    assert read.status_code == 200
    body = read.json()
    assert body["values"] == {
        "agent_max_execution_seconds": None,
        "agent_max_model_calls": None,
        "max_running_runs_per_caller": None,
        "max_running_runs": None,
    }
    assert body["defaults"]["agent_max_execution_seconds"] == 900
    assert body["defaults"]["agent_max_model_calls"] == 20
    assert body["defaults"]["max_running_runs_per_caller"] == 3
    assert body["defaults"]["max_running_runs"] is None
    assert body["ceilings"]["agent_max_execution_seconds"] == 7200
    assert body["ceilings"]["agent_max_model_calls"] == 200


async def test_an_administrator_sets_them_and_it_is_audited(
    client: TestClient, workspace_id: str, scope: dict[str, str], engine: AsyncEngine
) -> None:
    changed = _set(
        client,
        workspace_id,
        scope,
        agent_max_execution_seconds=3600,
        agent_max_model_calls=60,
        max_running_runs_per_caller=5,
        max_running_runs=40,
    )

    assert changed.status_code == 200, changed.text
    assert _limits(client, workspace_id, scope).json()["values"] == {
        "agent_max_execution_seconds": 3600,
        "agent_max_model_calls": 60,
        "max_running_runs_per_caller": 5,
        "max_running_runs": 40,
    }
    async with engine.connect() as connection:
        stored = (
            await connection.execute(
                text(
                    "SELECT max_running_runs_per_caller, max_running_runs FROM workspaces "
                    "WHERE id = :id"
                ),
                {"id": UUID(workspace_id)},
            )
        ).one()
        audited = await connection.scalar(
            text("SELECT count(*) FROM audit_events WHERE action = 'workspace.limits_changed'")
        )
    assert tuple(stored) == (5, 40), "the claim reads these columns"
    assert audited == 1


def test_null_puts_a_limit_back_to_its_default(
    client: TestClient, workspace_id: str, scope: dict[str, str]
) -> None:
    _set(client, workspace_id, scope, agent_max_model_calls=60)

    _set(client, workspace_id, scope, agent_max_model_calls=None)

    assert _limits(client, workspace_id, scope).json()["values"]["agent_max_model_calls"] is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent_max_execution_seconds", 7201),
        ("agent_max_model_calls", 201),
        ("max_running_runs_per_caller", 0),
        ("max_running_runs", -1),
    ],
)
def test_a_value_outside_what_the_platform_allows_is_refused(
    client: TestClient, workspace_id: str, scope: dict[str, str], field: str, value: int
) -> None:
    refused = _set(client, workspace_id, scope, **{field: value})

    assert refused.status_code == 422
    assert _limits(client, workspace_id, scope).json()["values"][field] is None


async def test_a_developer_reads_the_limits_but_cannot_change_them(
    client: TestClient, workspace_id: str, scope: dict[str, str], engine: AsyncEngine
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE memberships SET role = 'developer' WHERE workspace_id = :id"),
            {"id": UUID(workspace_id)},
        )
        await connection.execute(text("UPDATE users SET is_platform_admin = false"))

    assert _limits(client, workspace_id, scope).status_code == 200
    assert _set(client, workspace_id, scope, max_running_runs=5).status_code == 403


def test_an_agent_may_not_ask_for_longer_than_its_workspace_allows(
    client: TestClient, workspace_id: str, scope: dict[str, str]
) -> None:
    refused = _draft(client, scope, max_execution_seconds=1800)
    assert refused.status_code == 422
    assert refused.json()["code"] == "execution_ceiling_exceeded"
    assert refused.json()["context"] == {"asked": 1800, "allowed": 900}
    assert "900" in refused.json()["detail"]

    _set(client, workspace_id, scope, agent_max_execution_seconds=3600)

    assert _draft(client, scope, max_execution_seconds=1800).status_code == 200


def test_an_agent_may_ask_for_more_rounds_once_its_workspace_allows_them(
    client: TestClient, workspace_id: str, scope: dict[str, str]
) -> None:
    assert _draft(client, scope, max_model_calls=60).status_code == 422

    _set(client, workspace_id, scope, agent_max_model_calls=60)

    assert _draft(client, scope, max_model_calls=60).status_code == 200


def test_too_many_model_calls_is_refused_with_both_numbers_in_context(
    client: TestClient, workspace_id: str, scope: dict[str, str]
) -> None:
    refused = _draft(client, scope, max_model_calls=21)

    assert refused.status_code == 422
    assert refused.json()["code"] == "round_ceiling_exceeded"
    assert refused.json()["context"] == {"asked": 21, "allowed": 20}


def test_too_many_tool_calls_is_refused_with_both_numbers_in_context(
    client: TestClient, workspace_id: str, scope: dict[str, str]
) -> None:
    refused = _draft(client, scope, max_tool_calls=201)

    assert refused.status_code == 422
    assert refused.json()["code"] == "tool_call_ceiling_exceeded"
    assert refused.json()["context"] == {"asked": 201, "allowed": 200}
