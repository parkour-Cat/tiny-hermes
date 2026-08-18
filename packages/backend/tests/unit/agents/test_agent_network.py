"""What an Agent may ask the network for, and what publish refuses.

Two things pinned here, both the same shape as the skill bindings next door.

The widening promise, for the fifth time: a spec that asks for no network
normalizes to the same bytes with the same content hash, so `schema_version`
stays 1 and no published version is migrated.

And §16.5's rule that a layer may narrow and never widen — caught at publish
rather than at the connection, because an entry that can never match is a line
in an immutable version that reads like a permission and is not one.
"""

from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from tiny_hermes.agents.application.service import (
    AgentCatalog,
    AgentNetworkOutsideWorkspace,
    AgentNetworkUnavailable,
)
from tiny_hermes.agents.domain.models import (
    MAX_NETWORK_ENTRIES,
    AgentSpec,
    AgentVersion,
    normalize_agent_spec,
)
from tiny_hermes.agents.infrastructure.memory_store import MemoryAgentStore
from tiny_hermes.outbound.domain.scope import OutboundScope
from tiny_hermes.tenancy.domain.models import Actor, Role

from .test_agent_models import valid_spec
from .test_model_policy import DETERMINISTIC_HASH

# -- the widening ------------------------------------------------------------


def test_a_spec_that_asks_for_no_network_hashes_to_what_it_did() -> None:
    spec = AgentSpec.model_validate(valid_spec())
    document, content_hash = normalize_agent_spec(spec)

    assert content_hash == DETERMINISTIC_HASH
    assert "network" not in document
    assert spec.schema_version == 1
    assert spec.network is None


def test_a_declared_network_survives_normalization() -> None:
    spec = AgentSpec.model_validate(
        {**valid_spec(), "network": {"allow": ["api.example.com"]}}
    )

    document, content_hash = normalize_agent_spec(spec)

    assert document["network"] == {"allow": ["api.example.com"]}
    assert content_hash != DETERMINISTIC_HASH


# -- what an author may write ------------------------------------------------


def test_an_entry_nobody_could_review_is_refused_by_the_spec_itself() -> None:
    """The same parser every level uses, so what an author may write and what a
    connection is measured against cannot drift apart."""
    for entry in ("*", "*.com", "http://api.example.com", "api.example.com:443"):
        with pytest.raises(ValidationError):
            AgentSpec.model_validate({**valid_spec(), "network": {"allow": [entry]}})


def test_a_target_named_twice_is_refused() -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(
            {**valid_spec(), "network": {"allow": ["api.example.com"] * 2}}
        )


def test_more_targets_than_anyone_would_review_are_refused() -> None:
    many = [f"host{index}.example.com" for index in range(MAX_NETWORK_ENTRIES + 1)]

    with pytest.raises(ValidationError):
        AgentSpec.model_validate({**valid_spec(), "network": {"allow": many}})


def test_an_entry_is_stored_in_one_spelling() -> None:
    spec = AgentSpec.model_validate(
        {**valid_spec(), "network": {"allow": ["API.Example.COM."]}}
    )

    assert spec.network is not None
    assert spec.network.allow == ("api.example.com",)


# -- and what publish does with it -------------------------------------------


@dataclass
class Scopes:
    """The smallest reader the publish check needs."""

    approved: dict[UUID, OutboundScope] = field(
        default_factory=dict[UUID, OutboundScope]
    )

    async def workspace(self, workspace_id: UUID) -> OutboundScope:
        return self.approved.get(workspace_id, OutboundScope.nothing())


@dataclass
class Publishing:
    catalog: AgentCatalog
    workspace_id: UUID
    actor: Actor
    agent_id: UUID
    revision: int

    async def publish(self) -> AgentVersion:
        result = await self.catalog.publish(
            self.workspace_id, self.actor, self.agent_id, self.revision, "req-3"
        )
        return result.version


async def publishing_with(
    *allow: str, approved: tuple[str, ...] = (), reader: bool = True
) -> Publishing:
    workspace_id = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(workspace_id, actor.id)] = Role.DEVELOPER
    scopes = Scopes({workspace_id: OutboundScope.of(approved)})
    catalog = AgentCatalog(store, scopes=scopes if reader else None)
    agent = await catalog.create_agent(workspace_id, actor, "Analyst", "analyst", "req-1")
    draft = await catalog.replace_draft(
        workspace_id,
        actor,
        agent.id,
        1,
        {**valid_spec(), "network": {"allow": list(allow)}},
        "req-2",
    )
    return Publishing(catalog, workspace_id, actor, agent.id, draft.revision)


async def test_a_target_the_workspace_approved_publishes() -> None:
    publishing = await publishing_with(
        "api.example.com", approved=("*.example.com",)
    )

    published = await publishing.publish()

    assert published.version_number == 1
    assert published.spec["network"] == {"allow": ["api.example.com"]}


async def test_a_target_the_workspace_did_not_approve_is_refused() -> None:
    publishing = await publishing_with(
        "payments.other.example", approved=("*.example.com",)
    )

    with pytest.raises(AgentNetworkOutsideWorkspace) as refused:
        await publishing.publish()

    assert refused.value.entries == ("payments.other.example",)


async def test_every_offending_target_comes_back_at_once() -> None:
    """Publishing four times to find four problems is how an author learns to
    stop reading the message."""
    publishing = await publishing_with(
        "api.example.com",
        "one.other.example",
        "two.other.example",
        approved=("*.example.com",),
    )

    with pytest.raises(AgentNetworkOutsideWorkspace) as refused:
        await publishing.publish()

    assert set(refused.value.entries) == {"one.other.example", "two.other.example"}


async def test_an_agent_may_not_widen_a_wildcard_its_workspace_holds() -> None:
    """`api.example.com` is inside `*.example.com`; the reverse is not."""
    publishing = await publishing_with(
        "*.example.com", approved=("api.example.com",)
    )

    with pytest.raises(AgentNetworkOutsideWorkspace):
        await publishing.publish()


async def test_a_workspace_that_approved_nothing_lets_no_agent_ask() -> None:
    publishing = await publishing_with("api.example.com")

    with pytest.raises(AgentNetworkOutsideWorkspace):
        await publishing.publish()


async def test_an_agent_asking_for_nothing_publishes_without_a_reader() -> None:
    """An Agent that never asked for the network does not need one, and does
    not get the network because its workspace has some."""
    workspace_id = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(workspace_id, actor.id)] = Role.DEVELOPER
    catalog = AgentCatalog(store, scopes=None)
    agent = await catalog.create_agent(workspace_id, actor, "Analyst", "analyst", "req-1")
    draft = await catalog.replace_draft(
        workspace_id, actor, agent.id, 1, valid_spec(), "req-2"
    )

    published = await catalog.publish(
        workspace_id, actor, agent.id, draft.revision, "req-3"
    )

    assert "network" not in published.version.spec


async def test_asking_for_the_network_where_scopes_are_not_wired_in_is_refused() -> None:
    """Refused rather than allowed: a platform that cannot check is a platform
    that must not publish the claim."""
    publishing = await publishing_with("api.example.com", reader=False)

    with pytest.raises(AgentNetworkUnavailable):
        await publishing.publish()
