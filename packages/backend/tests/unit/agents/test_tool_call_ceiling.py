"""How many tool calls an Agent author may ask for, and who decides.

`max_tool_calls` was `le=50`, a literal on the field — the same shape
`max_model_calls` had before design §4.7 moved its ceiling into the platform
administrator's hands. Fifty tool calls is a short task: an Agent that reads a
repository and edits a few files runs out mid-way and pauses at `limit`.

The same reasoning as `test_round_ceiling.py`: a `le=` on the field would make
every published version above a lowered ceiling a document that no longer
parses, so the field keeps only `ge=0` and the ceiling is checked where a
value is written. The default stays 50, so an Agent that never set it behaves
as before and hashes as before.
"""

from typing import cast
from uuid import uuid4

import pytest
from tiny_hermes.agents.application.service import AgentCatalog, ToolCallCeilingExceeded
from tiny_hermes.agents.domain.models import AgentLimits, PlatformCeilings
from tiny_hermes.agents.infrastructure.memory_store import MemoryAgentStore
from tiny_hermes.shared.config import Settings
from tiny_hermes.tenancy.domain.models import Actor, Role

from .test_agent_models import valid_spec


def asking_for(calls: int) -> dict[str, object]:
    spec = valid_spec()
    limits: dict[str, object] = {**cast("dict[str, object]", spec["limits"])}
    limits["max_tool_calls"] = calls
    spec["limits"] = limits
    return spec


async def _publish(spec: dict[str, object], ceiling: int) -> None:
    workspace_id = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(workspace_id, actor.id)] = Role.DEVELOPER
    catalog = AgentCatalog(store, ceilings=PlatformCeilings(max_tool_calls=ceiling))
    agent = await catalog.create_agent(workspace_id, actor, "Analyst", "analyst", "req-1")
    draft = await catalog.replace_draft(workspace_id, actor, agent.id, 1, spec, "req-2")
    await catalog.publish(workspace_id, actor, agent.id, draft.revision, "req-3")


def test_the_value_alone_is_no_longer_bounded_by_a_literal() -> None:
    assert AgentLimits(max_tool_calls=120).max_tool_calls == 120


def test_the_default_is_still_fifty() -> None:
    assert AgentLimits().max_tool_calls == 50


async def test_a_raised_ceiling_lets_the_author_ask_for_more() -> None:
    await _publish(asking_for(150), ceiling=200)


async def test_asking_above_the_ceiling_is_refused_with_both_numbers() -> None:
    with pytest.raises(ToolCallCeilingExceeded) as refused:
        await _publish(asking_for(150), ceiling=100)

    assert refused.value.asked == 150
    assert refused.value.allowed == 100


def test_the_platform_ceiling_is_configurable_and_above_the_default() -> None:
    assert Settings.model_fields["agent_max_tool_calls"].default == 200
