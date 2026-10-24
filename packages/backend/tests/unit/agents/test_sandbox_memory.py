"""How much sandbox memory an Agent may ask for (§21.3, v2.22).

The real-model long-task probe (2026-10-23) found sandboxes using a few MiB
each while every one reserved the instance's 1 GiB, so a host admitted three
at once. How heavy a sandbox needs to be is the Agent's to say.

The widening promise again: a spec that declares no `sandbox` normalizes to
the same bytes and the same content hash it always did. The value has a floor
and no ceiling literal, for the reason `max_model_calls` has none: the
ceiling is checked where a value is written, never where a published version
is read back.
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError
from tiny_hermes.agents.application.service import AgentCatalog, SandboxMemoryCeilingExceeded
from tiny_hermes.agents.domain.models import (
    AgentSandbox,
    AgentSpec,
    PlatformCeilings,
    normalize_agent_spec,
)
from tiny_hermes.agents.infrastructure.memory_store import MemoryAgentStore
from tiny_hermes.shared.config import Settings
from tiny_hermes.tenancy.domain.models import Actor, Role

from .test_agent_models import valid_spec
from .test_model_policy import DETERMINISTIC_HASH


def asking_for(memory_mb: int) -> dict[str, object]:
    return {**valid_spec(), "sandbox": {"memory_mb": memory_mb}}


async def _publish(spec: dict[str, object], ceiling: int) -> None:
    workspace_id = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(workspace_id, actor.id)] = Role.DEVELOPER
    catalog = AgentCatalog(store, ceilings=PlatformCeilings(sandbox_memory_mb=ceiling))
    agent = await catalog.create_agent(workspace_id, actor, "Analyst", "analyst", "req-1")
    draft = await catalog.replace_draft(workspace_id, actor, agent.id, 1, spec, "req-2")
    await catalog.publish(workspace_id, actor, agent.id, draft.revision, "req-3")


def test_a_spec_that_declares_no_sandbox_hashes_to_what_it_did() -> None:
    document, content_hash = normalize_agent_spec(AgentSpec.model_validate(valid_spec()))

    assert content_hash == DETERMINISTIC_HASH
    assert "sandbox" not in document


def test_a_sandbox_without_a_memory_size_hashes_as_no_sandbox() -> None:
    """`{}` and an absent key mean the same thing, so they must not publish
    as two different versions."""
    document, content_hash = normalize_agent_spec(
        AgentSpec.model_validate({**valid_spec(), "sandbox": {}})
    )

    assert content_hash == DETERMINISTIC_HASH
    assert "sandbox" not in document


def test_a_declared_size_is_kept() -> None:
    document, content_hash = normalize_agent_spec(AgentSpec.model_validate(asking_for(256)))

    assert document["sandbox"] == {"memory_mb": 256}
    assert content_hash != DETERMINISTIC_HASH


def test_below_the_floor_is_refused_by_the_document_itself() -> None:
    with pytest.raises(ValidationError):
        AgentSandbox(memory_mb=255)


def test_the_value_alone_is_not_bounded_by_a_literal() -> None:
    assert AgentSandbox(memory_mb=65_536).memory_mb == 65_536


async def test_up_to_the_ceiling_publishes() -> None:
    await _publish(asking_for(4096), ceiling=4096)


async def test_above_the_ceiling_is_refused_with_both_numbers() -> None:
    with pytest.raises(SandboxMemoryCeilingExceeded) as refused:
        await _publish(asking_for(8192), ceiling=4096)

    assert refused.value.asked == 8192
    assert refused.value.allowed == 4096


def test_the_platform_ceiling_is_configurable_and_starts_at_four_gib() -> None:
    assert Settings.model_fields["sandbox_ceiling_memory_mb"].default == 4096
