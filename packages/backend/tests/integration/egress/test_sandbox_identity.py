"""A sandbox's identity, written by the Controller and read by the proxy.

A container presents nothing — a process inside one that holds a credential is
a process that can lend it — so the proxy answers "who is this" from the
address its packets came from. That only works if the two ends agree, and they
are in different processes with a table between them. This is that table, from
both sides.

The Run's workspace and Agent Version come from the Run rather than from the
registration, so nothing has to keep two copies of them agreeing.
"""

from ipaddress import ip_address
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.egress.infrastructure.sql_directory import SqlScopeDirectory
from tiny_hermes.outbound.domain.scope import OutboundScope
from tiny_hermes.sandbox.infrastructure.sql_store import SqlSandboxStore

from ..conftest import VALID_SPEC

ADDRESS = "172.30.0.5"


async def _register(engine: AsyncEngine, run_id: str, address: str = ADDRESS) -> UUID:
    """What the Controller writes when a container gets an address."""
    sandbox_id = uuid4()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as session:
        await SqlSandboxStore(session).register_egress_address(
            address=address, run_id=UUID(run_id), sandbox_id=sandbox_id
        )
    return sandbox_id


def _run(client: TestClient, scope: dict[str, str], network: list[str]) -> dict[str, Any]:
    """A published Agent with a network section, and one Run of it."""
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
            "spec": {**VALID_SPEC, "network": {"allow": network}},
        },
    )
    assert draft.status_code == 200, draft.text
    published = client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=scope,
        json={"expected_revision": draft.json()["revision"]},
    )
    assert published.status_code == 201, published.text
    session_id = client.post(
        "/api/v1/sessions", headers=scope, json={"agent_id": agent_id}
    ).json()["id"]
    created = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": "sandbox-identity"},
        json={"session_id": session_id, "input": "hello"},
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


@pytest.fixture
def approved(client: TestClient, scope: dict[str, str], admin_csrf: str) -> None:
    """A platform that approved a wildcard and a workspace that chose inside it."""
    client.post(
        "/api/v1/outbound-scopes/platform",
        headers={"X-CSRF-Token": admin_csrf},
        json={"entry": "*.example.com"},
    )
    client.post(
        "/api/v1/outbound-scopes/workspace", headers=scope, json={"entry": "api.example.com"}
    )


async def test_an_unregistered_address_is_nobody(engine: AsyncEngine) -> None:
    """The refusal that matters most: an address nobody wrote down cannot even
    use the proxy as a resolver, because the caller is refused before its
    target is parsed."""
    directory = SqlScopeDirectory(async_sessionmaker(engine, expire_on_commit=False))

    assert await directory.sandbox_claim(ip_address("172.30.0.99")) is None


async def test_a_registered_address_is_the_run_that_holds_it(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, approved: None
) -> None:
    del approved
    run = _run(client, scope, ["api.example.com"])
    await _register(engine, run["id"])
    directory = SqlScopeDirectory(async_sessionmaker(engine, expire_on_commit=False))

    claim = await directory.sandbox_claim(ip_address(ADDRESS))

    assert claim is not None
    assert claim.kind.value == "sandbox"
    assert str(claim.run_id) == run["id"]
    # Not from the registration: the workspace and the version come from the
    # Run, so the sandbox is measured against what its Run is executing.
    assert claim.workspace_id is not None
    assert claim.agent_version_id is not None


async def test_the_layers_a_sandbox_gets_are_the_ones_its_run_was_published_with(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, approved: None
) -> None:
    """All the way through: platform ∩ workspace ∩ the Agent's own section."""
    del approved
    run = _run(client, scope, ["api.example.com"])
    await _register(engine, run["id"])
    directory = SqlScopeDirectory(async_sessionmaker(engine, expire_on_commit=False))
    claim = await directory.sandbox_claim(ip_address(ADDRESS))
    assert claim is not None

    layers = await directory.layers_for(claim)
    effective = layers.effective()

    assert effective.allows_host("api.example.com") is True
    # Approved by the platform and by the workspace's parent wildcard, and not
    # named by this Agent — so the Agent layer is what closes it.
    assert effective.allows_host("other.example.com") is False


async def test_an_agent_that_asked_for_nothing_reaches_nothing(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, approved: None
) -> None:
    """An Agent does not get the network because its workspace has some."""
    del approved
    run = _run(client, scope, [])
    await _register(engine, run["id"])
    directory = SqlScopeDirectory(async_sessionmaker(engine, expire_on_commit=False))
    claim = await directory.sandbox_claim(ip_address(ADDRESS))
    assert claim is not None

    assert (await directory.layers_for(claim)).effective().empty is True


async def test_one_address_belongs_to_one_container_at_a_time(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, approved: None
) -> None:
    """Docker reuses addresses. The container that has one now owns it, and the
    row a previous container left behind is exactly the row that must not
    survive — otherwise the next Run inherits somebody else's identity.
    """
    del approved
    run = _run(client, scope, ["api.example.com"])
    first = await _register(engine, run["id"])
    second = await _register(engine, run["id"])

    async with engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT sandbox_id FROM sandbox_egress_addresses WHERE address = :a"),
            {"a": ADDRESS},
        )
        owners = [row[0] for row in rows.all()]

    assert owners == [second]
    assert first not in owners


async def test_clearing_takes_the_identity_away(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, approved: None
) -> None:
    """What freeze and destroy do. A frozen instance may not open a new
    connection (§16.4), and a destroyed one must not lend its identity to
    whatever Docker gives the address to next."""
    del approved
    run = _run(client, scope, ["api.example.com"])
    sandbox_id = await _register(engine, run["id"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as session:
        await SqlSandboxStore(session).clear_egress_address(sandbox_id)

    directory = SqlScopeDirectory(sessions)

    assert await directory.sandbox_claim(ip_address(ADDRESS)) is None


async def test_a_platform_that_approved_nothing_lets_a_sandbox_reach_nothing(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine
) -> None:
    """No fixture here: the platform scope is empty, which is the default."""
    run = _run(client, scope, [])
    await _register(engine, run["id"])
    directory = SqlScopeDirectory(async_sessionmaker(engine, expire_on_commit=False))
    claim = await directory.sandbox_claim(ip_address(ADDRESS))
    assert claim is not None

    layers = await directory.layers_for(claim)

    assert layers.platform == OutboundScope.nothing()
    assert layers.effective().empty is True
