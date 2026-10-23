"""Product design §23.17 (v2.20): the database keeps workspaces apart when
the code forgets to.

Each test here takes the code's filter away, or goes around it, and checks
that the database still answers for one workspace only.
"""

from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.agents.infrastructure import sql_store as agent_store
from tiny_hermes.agents.infrastructure.tables import AgentRow
from tiny_hermes.api.app import create_app
from tiny_hermes.shared.config import Settings
from tiny_hermes.shared.database import Base, build_session_factory
from tiny_hermes.shared.tenancy_scope import acting_in

APP_ROLE = "tiny_hermes_app"


def _agent(client: TestClient, headers: dict[str, str], name: str) -> str:
    created = client.post(
        "/api/v1/agents", headers=headers, json={"name": name, "alias": name.lower()}
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


@pytest.fixture
def two_workspaces(
    client: TestClient, scope: dict[str, str], admin_csrf: str
) -> tuple[dict[str, str], dict[str, str]]:
    other = client.post(
        "/api/v1/workspaces", headers={"X-CSRF-Token": admin_csrf}, json={"name": "Other"}
    )
    assert other.status_code == 201
    second = {"X-Workspace-Id": str(other.json()["id"]), "X-CSRF-Token": admin_csrf}
    _agent(client, scope, "Ours")
    _agent(client, second, "Theirs")
    return scope, second


def _tenant_tables() -> list[str]:
    # Every table the application registers (importing `create_app` above
    # registers every module's), so a table added later is covered by this
    # test without anyone remembering to list it.
    assert create_app
    return sorted(
        table.name for table in Base.metadata.tables.values() if "workspace_id" in table.c
    )


async def test_every_table_with_a_workspace_has_row_level_security(engine: AsyncEngine) -> None:
    tables = _tenant_tables()
    assert len(tables) >= 31
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, "
                    "(SELECT count(*) FROM pg_policies p WHERE p.tablename = c.relname) "
                    "FROM pg_class c WHERE c.relname = ANY(:names) AND c.relkind = 'r'"
                ),
                {"names": tables},
            )
        ).all()
    found = {name: (enabled, policies) for name, enabled, policies in rows}
    assert sorted(found) == tables
    unprotected = [
        name for name, (enabled, policies) in found.items() if not enabled or not policies
    ]
    assert unprotected == []


async def test_the_application_role_cannot_go_around_it(engine: AsyncEngine) -> None:
    """A superuser, an owner or BYPASSRLS ignores every policy."""
    async with engine.connect() as connection:
        role = (
            await connection.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :name"),
                {"name": APP_ROLE},
            )
        ).one()
        owned = await connection.scalar(
            text(
                "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' "
                "AND tableowner = :name"
            ),
            {"name": APP_ROLE},
        )
    assert tuple(role) == (False, False)
    assert owned == 0


async def test_the_application_connects_as_that_role(settings: Settings) -> None:
    sessions = build_session_factory(settings)
    async with sessions() as session:
        assert await session.scalar(text("SELECT current_user")) == APP_ROLE
    await sessions.kw["bind"].dispose()


async def test_a_query_without_a_workspace_filter_sees_only_its_workspace(
    two_workspaces: tuple[dict[str, str], dict[str, str]], settings: Settings
) -> None:
    ours, _ = two_workspaces
    sessions = build_session_factory(settings)
    with acting_in(UUID(ours["X-Workspace-Id"])):
        async with sessions() as session:
            names = (await session.scalars(select(AgentRow.name))).all()
    async with sessions() as session:
        everyone = (await session.scalars(select(AgentRow.name))).all()
    await sessions.kw["bind"].dispose()

    assert names == ["Ours"]
    assert sorted(everyone) == ["Ours", "Theirs"], "outside a workspace nothing is filtered"


async def test_a_write_into_another_workspace_is_refused(
    two_workspaces: tuple[dict[str, str], dict[str, str]], settings: Settings
) -> None:
    ours, theirs = two_workspaces
    sessions = build_session_factory(settings)
    with acting_in(UUID(ours["X-Workspace-Id"])), pytest.raises(DBAPIError) as refused:
        async with sessions.begin() as session:
            await session.execute(
                text(
                    "INSERT INTO agents (id, workspace_id, name, alias, status, created_at) "
                    "VALUES (:id, :workspace, 'Planted', 'planted', 'draft', now())"
                ),
                {"id": uuid4(), "workspace": UUID(theirs["X-Workspace-Id"])},
            )
    await sessions.kw["bind"].dispose()

    assert "row-level security" in str(refused.value)


async def test_platform_rows_stay_visible_inside_a_workspace(
    two_workspaces: tuple[dict[str, str], dict[str, str]], settings: Settings
) -> None:
    """A null workspace belongs to no other tenant: the bootstrap's audit row."""
    ours, _ = two_workspaces
    sessions = build_session_factory(settings)
    with acting_in(UUID(ours["X-Workspace-Id"])):
        async with sessions() as session:
            platform = await session.scalar(
                text("SELECT count(*) FROM audit_events WHERE workspace_id IS NULL")
            )
    await sessions.kw["bind"].dispose()

    assert platform and platform > 0


async def test_an_api_request_whose_query_forgot_its_workspace_still_sees_one(
    two_workspaces: tuple[dict[str, str], dict[str, str]],
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug this layer exists for, through a real request: the list query
    loses its `WHERE workspace_id = ...`, and the answer is still one
    workspace's agents."""
    ours, _ = two_workspaces

    async def forgetful(self: Any, workspace_id: UUID) -> Sequence[Any]:
        del workspace_id
        rows = (await self._session.scalars(select(AgentRow).order_by(AgentRow.name))).all()
        return [agent_store._agent(row) for row in rows]  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setattr(agent_store.SqlAgentStore, "list_agents", forgetful)

    listed = client.get("/api/v1/agents", headers=ours)

    assert listed.status_code == 200
    assert [agent["name"] for agent in listed.json()] == ["Ours"]



async def test_a_connection_that_was_scoped_serves_an_unscoped_transaction_next(
    two_workspaces: tuple[dict[str, str], dict[str, str]], settings: Settings
) -> None:
    """The setting is transaction-local; after it ends the connection holds an
    empty string, not nothing. The policy's cast once failed on exactly that,
    and 120 tests went red: one connection, so the second transaction is sure
    to reuse the first one's."""
    ours, _ = two_workspaces
    sessions = build_session_factory(settings, pool_size=1, max_overflow=0)
    with acting_in(UUID(ours["X-Workspace-Id"])):
        async with sessions() as session:
            assert (await session.scalars(select(AgentRow.name))).all() == ["Ours"]
    async with sessions() as session:
        everyone = (await session.scalars(select(AgentRow.name))).all()
    await sessions.kw["bind"].dispose()

    assert sorted(everyone) == ["Ours", "Theirs"]
