"""How many rounds an Agent author may ask for, and who decides.

Design §4.7. `max_model_calls` was `ge=1, le=20`, a literal chosen when a Run
could not run long. A Goal loop that must finish inside 20 model calls cannot
do much, and §12.3 puts that number in a platform administrator's hands.

The interesting part is not raising it. It is that the ceiling must live
somewhere a *published* version never passes through: an operator who lowers
the ceiling tomorrow must not turn every AgentVersion above it into a document
that no longer parses, which is what a `le=` on the field would do. So the
field keeps only `ge=1` and the ceiling is checked where a value is written —
saving a draft, and again at publish, because a draft saved yesterday was
checked against yesterday's ceiling.
"""

from dataclasses import dataclass
from typing import cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from tiny_hermes.agents.application.service import (
    AgentCatalog,
    RoundCeilingExceeded,
)
from tiny_hermes.agents.domain.models import (
    AgentLimits,
    AgentSpec,
    PlatformCeilings,
    normalize_agent_spec,
)
from tiny_hermes.agents.infrastructure.memory_store import MemoryAgentStore
from tiny_hermes.tenancy.domain.models import Actor, Role

from .test_agent_models import valid_spec
from .test_model_policy import DETERMINISTIC_HASH


def asking_for(rounds: int) -> dict[str, object]:
    spec = valid_spec()
    limits: dict[str, object] = {**cast("dict[str, object]", spec["limits"])}
    limits["max_model_calls"] = rounds
    spec["limits"] = limits
    return spec


@dataclass
class Where:
    """One workspace, one author, one Agent, and the store they live in."""

    store: MemoryAgentStore
    workspace_id: UUID
    actor: Actor
    agent_id: UUID


async def catalog_with(ceiling: int) -> tuple[AgentCatalog, Where]:
    workspace_id = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(workspace_id, actor.id)] = Role.DEVELOPER
    catalog = AgentCatalog(store, ceilings=PlatformCeilings(max_model_calls=ceiling))
    agent = await catalog.create_agent(workspace_id, actor, "Analyst", "analyst", "req-1")
    return catalog, Where(store, workspace_id, actor, agent.id)


# -- the ceiling is the platform's, not the field's -------------------------


def test_the_value_alone_is_no_longer_bounded_by_a_literal() -> None:
    """`AgentLimits` is what a stored document parses back into, so it has to
    accept every value any ceiling has ever allowed."""
    assert AgentLimits(max_model_calls=40).max_model_calls == 40
    assert AgentLimits(max_model_calls=500).max_model_calls == 500


def test_zero_rounds_is_still_refused_by_the_field() -> None:
    """Not a ceiling. An Agent that may make no model call cannot run at all,
    and no setting makes that a coherent thing to publish."""
    with pytest.raises(ValidationError):
        AgentLimits(max_model_calls=0)


def test_the_default_is_still_the_literal_twenty() -> None:
    """The default stays on the field even though §12.3 lets an administrator
    set one, because the default decides what an omitted `limits` normalizes
    to — moving it would give the same submitted spec a different content hash
    on different installations."""
    assert AgentLimits().max_model_calls == 20
    _, content_hash = normalize_agent_spec(AgentSpec.model_validate(valid_spec()))
    assert content_hash == DETERMINISTIC_HASH


# -- what an author may write ------------------------------------------------


async def test_a_raised_ceiling_lets_the_author_ask_for_more() -> None:
    catalog, where = await catalog_with(40)

    draft = await catalog.replace_draft(
        where.workspace_id, where.actor, where.agent_id, 1, asking_for(40), "req-2"
    )

    assert draft.spec.limits.max_model_calls == 40


async def test_the_same_value_is_refused_under_a_lower_ceiling() -> None:
    catalog, where = await catalog_with(10)

    with pytest.raises(RoundCeilingExceeded):
        await catalog.replace_draft(
            where.workspace_id, where.actor, where.agent_id, 1, asking_for(40), "req-2"
        )


async def test_a_lowered_ceiling_can_refuse_even_the_field_default() -> None:
    """20 is only the default. An administrator who set the ceiling to 10 meant
    it, and a spec that spells out 20 is above it like any other value."""
    catalog, where = await catalog_with(10)

    with pytest.raises(RoundCeilingExceeded):
        await catalog.replace_draft(
            where.workspace_id, where.actor, where.agent_id, 1, asking_for(20), "req-2"
        )


async def test_the_ceiling_itself_is_allowed() -> None:
    catalog, where = await catalog_with(40)

    draft = await catalog.replace_draft(
        where.workspace_id, where.actor, where.agent_id, 1, asking_for(40), "req-2"
    )

    assert draft.spec.limits.max_model_calls == 40


# -- and what may still be published, or still run ---------------------------


async def test_a_draft_saved_under_a_higher_ceiling_cannot_be_published_after_it_drops() -> (
    None
):
    """Publish is the last moment a mistake is cheap, and the draft it publishes
    was checked against a ceiling that no longer applies."""
    catalog, where = await catalog_with(40)
    draft = await catalog.replace_draft(
        where.workspace_id, where.actor, where.agent_id, 1, asking_for(40), "req-2"
    )
    # The operator lowered it and the process restarted around the same store.
    after = AgentCatalog(where.store, ceilings=PlatformCeilings(max_model_calls=10))

    with pytest.raises(RoundCeilingExceeded):
        await after.publish(
            where.workspace_id, where.actor, where.agent_id, draft.revision, "req-3"
        )


def test_a_published_version_above_todays_ceiling_still_parses() -> None:
    """The promise the whole shape exists for.

    An AgentVersion is immutable and a Run loads it by parsing its document. If
    the ceiling lived on the field, lowering it would stop every Run of every
    Agent published above the new number — a configuration change that breaks
    running work, which §4.7 rules out.
    """
    spec = AgentSpec.model_validate(asking_for(40))

    assert spec.limits.max_model_calls == 40


def test_a_completion_condition_may_not_outlive_the_budget_it_was_given() -> None:
    """The cross-field rule from step 2 reads the spec's own value, not the
    platform's, so a raised ceiling does not quietly loosen it."""
    spec = asking_for(40)
    spec["tools"] = ["shell.exec"]
    spec["completion"] = {
        "verification_command": "pytest -q",
        "stop_conditions": {"max_rounds": 41},
    }

    with pytest.raises(ValidationError):
        AgentSpec.model_validate(spec)
