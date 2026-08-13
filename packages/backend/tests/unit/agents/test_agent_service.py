from uuid import uuid4

import pytest
from tiny_hermes.agents.application.service import (
    AgentCatalog,
    DraftRevisionConflict,
    ForbiddenAgentAction,
    InvalidAgentAlias,
    UnknownAgent,
)
from tiny_hermes.agents.infrastructure.memory_store import MemoryAgentStore
from tiny_hermes.tenancy.domain.models import Actor, Role

from .test_agent_models import valid_spec


async def test_publish_is_immutable_and_unchanged_publish_reuses_current_version() -> None:
    workspace_id = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(workspace_id, actor.id)] = Role.DEVELOPER
    catalog = AgentCatalog(store)

    agent = await catalog.create_agent(workspace_id, actor, "Analyst", "analyst", "req-1")
    draft = await catalog.replace_draft(
        workspace_id, actor, agent.id, 1, valid_spec(), "req-2"
    )
    first = await catalog.publish(
        workspace_id, actor, agent.id, draft.revision, "req-3"
    )
    repeated = await catalog.publish(
        workspace_id, actor, agent.id, draft.revision, "req-4"
    )

    assert first.version.id == repeated.version.id
    assert first.version.version_number == 1
    assert first.unchanged is False
    assert repeated.unchanged is True
    assert len(store.versions) == 1


async def test_stale_draft_revision_is_rejected_without_overwrite() -> None:
    workspace_id = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(workspace_id, actor.id)] = Role.WORKSPACE_ADMIN
    catalog = AgentCatalog(store)
    agent = await catalog.create_agent(workspace_id, actor, "Analyst", "analyst", "req-1")

    await catalog.replace_draft(workspace_id, actor, agent.id, 1, valid_spec(), "req-2")
    with pytest.raises(DraftRevisionConflict):
        await catalog.replace_draft(
            workspace_id, actor, agent.id, 1, valid_spec(), "req-3"
        )


async def test_viewer_cannot_modify_or_publish_agent() -> None:
    workspace_id = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(workspace_id, actor.id)] = Role.VIEWER
    catalog = AgentCatalog(store)

    with pytest.raises(ForbiddenAgentAction):
        await catalog.create_agent(workspace_id, actor, "Analyst", "analyst", "req-1")


async def test_changed_draft_publishes_a_new_version_and_rollback_keeps_history() -> None:
    workspace_id = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(workspace_id, actor.id)] = Role.DEVELOPER
    catalog = AgentCatalog(store)

    agent = await catalog.create_agent(workspace_id, actor, "Analyst", "analyst", "req-1")
    first_draft = await catalog.replace_draft(
        workspace_id, actor, agent.id, 1, valid_spec(), "req-2"
    )
    first = (
        await catalog.publish(workspace_id, actor, agent.id, first_draft.revision, "req-3")
    ).version

    changed = {**valid_spec(), "personality": "You are thorough."}
    second_draft = await catalog.replace_draft(
        workspace_id, actor, agent.id, first_draft.revision, changed, "req-4"
    )
    second = (
        await catalog.publish(workspace_id, actor, agent.id, second_draft.revision, "req-5")
    ).version

    assert second.version_number == 2
    assert second.content_hash != first.content_hash
    assert len(store.versions) == 2

    restored = await catalog.activate_version(workspace_id, actor, agent.id, first.id, "req-6")
    assert restored.id == first.id
    assert len(store.versions) == 2
    reloaded = await catalog.get_agent(workspace_id, actor, agent.id, "req-7")
    assert reloaded.current_version_id == first.id
    assert reloaded.status == "published"


async def test_cross_workspace_identifiers_are_unknown_rather_than_readable() -> None:
    first_workspace = uuid4()
    second_workspace = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(first_workspace, actor.id)] = Role.DEVELOPER
    store.roles[(second_workspace, actor.id)] = Role.DEVELOPER
    catalog = AgentCatalog(store)

    agent = await catalog.create_agent(first_workspace, actor, "Analyst", "analyst", "req-1")

    with pytest.raises(UnknownAgent):
        await catalog.get_agent(second_workspace, actor, agent.id, "req-2")
    with pytest.raises(UnknownAgent):
        await catalog.replace_draft(
            second_workspace, actor, agent.id, 1, valid_spec(), "req-2"
        )


async def test_platform_admin_without_membership_is_allowed_and_audited() -> None:
    workspace_id = uuid4()
    actor = Actor(uuid4(), True)
    store = MemoryAgentStore()
    catalog = AgentCatalog(store)

    agent = await catalog.create_agent(workspace_id, actor, "Analyst", "analyst", "req-1")

    assert agent.workspace_id == workspace_id
    assert "agent.created_by_platform_admin" in store.audit_actions


async def test_published_alias_names_the_current_version() -> None:
    workspace_id = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(workspace_id, actor.id)] = Role.DEVELOPER
    catalog = AgentCatalog(store)

    agent = await catalog.create_agent(workspace_id, actor, "Analyst", "analyst", "req-1")
    draft = await catalog.replace_draft(
        workspace_id, actor, agent.id, 1, valid_spec(), "req-2"
    )
    published = await catalog.publish(
        workspace_id, actor, agent.id, draft.revision, "req-3"
    )

    found_agent, found_version = await catalog.published_alias(
        workspace_id, actor, "analyst", "req-4"
    )
    assert found_agent.id == agent.id
    assert found_version.id == published.version.id

    with pytest.raises(UnknownAgent):
        await catalog.published_alias(workspace_id, actor, "missing", "req-5")


async def test_a_service_account_can_resolve_a_published_alias() -> None:
    workspace_id = uuid4()
    author = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(workspace_id, author.id)] = Role.DEVELOPER
    catalog = AgentCatalog(store)
    agent = await catalog.create_agent(workspace_id, author, "Analyst", "analyst", "req-1")
    draft = await catalog.replace_draft(
        workspace_id, author, agent.id, 1, valid_spec(), "req-2"
    )
    await catalog.publish(workspace_id, author, agent.id, draft.revision, "req-3")

    machine = Actor(
        uuid4(), False, is_service_account=True, role=Role.DEVELOPER
    )
    found_agent, _version = await catalog.published_alias(
        workspace_id, machine, "analyst", "req-4"
    )
    assert found_agent.id == agent.id


async def test_alias_must_be_lowercase_slug() -> None:
    workspace_id = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(workspace_id, actor.id)] = Role.DEVELOPER
    catalog = AgentCatalog(store)

    for alias in ("Analyst", "an alias", "-analyst", "analyst-", "a" * 81, ""):
        with pytest.raises(InvalidAgentAlias):
            await catalog.create_agent(workspace_id, actor, "Analyst", alias, "req-1")
