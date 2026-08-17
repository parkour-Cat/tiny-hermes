"""What an Agent author may say about context, and what publish does with it.

Design §4.8 and product design §7.4.2. Two things are being pinned here.

The first is the widening promise, for the third time: `tools` made it,
`completion` made it, and `context_budget` makes it again — a spec that
declares none normalizes to the same bytes with the same content hash, so
`schema_version` stays 1 and no published version is migrated.

The second is that the advice in a `context_budget_unsatisfied` refusal is
advice. §7.4.2 says it 不会静默生效, and the difference between telling an author
their tool schema budget has to come down and quietly cutting it is the
difference between an Agent they published and one they did not.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from tiny_hermes.agents.application.service import (
    AgentCatalog,
    ContextBudgetUnsatisfied,
    ContextWindowTooSmall,
)
from tiny_hermes.agents.domain.models import (
    AgentSpec,
    ContextBudget,
    normalize_agent_spec,
)
from tiny_hermes.agents.infrastructure.memory_store import MemoryAgentStore
from tiny_hermes.model_catalog.domain.models import (
    EndpointStatus,
    ModelEndpoint,
    ModelEndpointSpec,
    UsageQuality,
)
from tiny_hermes.runs.domain.context_budget import (
    DEFAULT_SEGMENTS,
    Accounting,
    ContextWindow,
    SegmentName,
    fit_budget,
)
from tiny_hermes.tenancy.domain.models import Actor, Role

from .test_agent_models import valid_spec
from .test_model_policy import DETERMINISTIC_HASH

# -- the widening ------------------------------------------------------------


def test_a_spec_that_declares_no_budget_hashes_to_what_it_did() -> None:
    spec = AgentSpec.model_validate(valid_spec())
    document, content_hash = normalize_agent_spec(spec)

    assert content_hash == DETERMINISTIC_HASH
    assert "context_budget" not in document
    assert spec.schema_version == 1
    assert spec.context_budget is None


def test_a_declared_budget_survives_normalization() -> None:
    spec = AgentSpec.model_validate(
        {
            **valid_spec(),
            "context_budget": {
                "segments": [{"segment": "memory", "target_tokens": 2_048}]
            },
        }
    )

    document, content_hash = normalize_agent_spec(spec)

    assert document["context_budget"] == {
        "segments": [{"segment": "memory", "target_tokens": 2_048, "max_tokens": None}]
    }
    assert content_hash != DETERMINISTIC_HASH


# -- what an author may write ------------------------------------------------


def test_an_override_stays_under_the_platform_cap() -> None:
    """§7.4.2 gives the hard cap to the administrator and the adjustment to the
    author, inside it. 3072 is memory's cap in the default table."""
    assert DEFAULT_SEGMENTS[SegmentName.MEMORY].max_tokens == 3_072
    with pytest.raises(ValidationError):
        ContextBudget.model_validate(
            {"segments": [{"segment": "memory", "max_tokens": 4_096}]}
        )


def test_a_target_below_the_segments_floor_is_refused() -> None:
    """Not a smaller budget — an unreachable one. `min_tokens` is kept whatever
    the target says, so a target under it describes something that never happens."""
    with pytest.raises(ValidationError):
        ContextBudget.model_validate(
            {"segments": [{"segment": "safety_rules", "target_tokens": 128}]}
        )


def test_a_target_above_its_own_max_is_refused() -> None:
    with pytest.raises(ValidationError):
        ContextBudget.model_validate(
            {
                "segments": [
                    {"segment": "memory", "max_tokens": 1_024, "target_tokens": 2_048}
                ]
            }
        )


def test_recent_history_has_no_budget_to_set() -> None:
    """It is given 剩余空间, not a number. Setting one would be setting the size
    of what is left over after everything else took its share."""
    with pytest.raises(ValidationError):
        ContextBudget.model_validate(
            {"segments": [{"segment": "recent_history", "target_tokens": 4_096}]}
        )


def test_a_segment_may_be_adjusted_once() -> None:
    with pytest.raises(ValidationError):
        ContextBudget.model_validate(
            {
                "segments": [
                    {"segment": "memory", "target_tokens": 2_048},
                    {"segment": "memory", "target_tokens": 512},
                ]
            }
        )


def test_what_is_not_named_keeps_the_platform_default() -> None:
    budget = ContextBudget.model_validate(
        {"segments": [{"segment": "memory", "target_tokens": 2_048}]}
    )

    resolved = budget.resolve()

    assert resolved[SegmentName.MEMORY].target_tokens == 2_048
    assert resolved[SegmentName.MEMORY].max_tokens == 3_072
    assert resolved[SegmentName.SAFETY_RULES] == DEFAULT_SEGMENTS[SegmentName.SAFETY_RULES]


# -- the static fit ----------------------------------------------------------


def test_a_roomy_window_needs_no_advice() -> None:
    fit = fit_budget(ContextWindow(128_000, reserved_output_tokens=4_096))

    assert fit.floor_fits
    assert fit.targets_fit
    assert fit.advice == ()


def test_advice_scales_every_segment_between_its_floor_and_its_ask() -> None:
    """Proportional rather than largest-first: the table's numbers already say
    which segments this platform cares about, and taking the whole overage out
    of the biggest one would quietly reverse that."""
    fit = fit_budget(ContextWindow(8_192, reserved_output_tokens=4_096))

    assert fit.floor_fits
    assert not fit.targets_fit
    suggested = {advice.segment: advice.suggested for advice in fit.advice}
    assert sum(suggested.values()) <= fit.allowance
    for segment, value in suggested.items():
        assert DEFAULT_SEGMENTS[segment].min_tokens <= value
        assert value < (DEFAULT_SEGMENTS[segment].target_tokens or 0)


def test_a_window_that_cannot_hold_the_minimum_gets_no_advice() -> None:
    """Nothing an author scales would help, and a suggestion that still does not
    fit is worse than none."""
    fit = fit_budget(ContextWindow(1_000, reserved_output_tokens=900))

    assert not fit.floor_fits
    assert fit.advice == ()


def test_separate_accounting_leaves_the_whole_window_for_input() -> None:
    """The same endpoint, refused under one declaration and published under the
    other. This is why §7.4.2 will not let the adapter guess it."""
    shared = fit_budget(ContextWindow(16_000, reserved_output_tokens=8_000))
    separate = fit_budget(
        ContextWindow(16_000, reserved_output_tokens=8_000, accounting=Accounting.SEPARATE)
    )

    assert shared.allowance == 8_000
    assert separate.allowance == 16_000
    assert not shared.targets_fit
    assert separate.targets_fit


# -- and what publish does with it -------------------------------------------


@dataclass
class Endpoints:
    """The smallest store the catalog's endpoint check needs."""

    endpoint: ModelEndpoint

    async def read(self, endpoint_id: UUID) -> ModelEndpoint | None:
        return self.endpoint if endpoint_id == self.endpoint.id else None


def endpoint_with(context_window: int, max_output_tokens: int) -> ModelEndpoint:
    now = datetime.now(UTC)
    return ModelEndpoint(
        id=uuid4(),
        spec=ModelEndpointSpec(
            name="acme-gpt",
            base_url="https://models.example.com/v1",
            model="acme-large",
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            usage_quality=UsageQuality.PROVIDER,
            credential_ref="TINY_HERMES_MODEL_KEY_ACME",
        ),
        status=EndpointStatus.ACTIVE,
        created_by=uuid4(),
        created_at=now,
        updated_at=now,
    )


async def publishing_against(
    endpoint: ModelEndpoint, budget: object | None = None
) -> tuple[AgentCatalog, UUID, Actor, UUID, int]:
    workspace_id = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(workspace_id, actor.id)] = Role.DEVELOPER
    catalog = AgentCatalog(store, endpoints=Endpoints(endpoint))  # type: ignore[arg-type]
    agent = await catalog.create_agent(workspace_id, actor, "Analyst", "analyst", "req-1")
    spec: dict[str, object] = {
        **valid_spec(),
        "model_policy": {
            "provider": "openai_compatible",
            "endpoint_id": str(endpoint.id),
        },
    }
    if budget is not None:
        spec["context_budget"] = budget
    draft = await catalog.replace_draft(workspace_id, actor, agent.id, 1, spec, "req-2")
    return catalog, workspace_id, actor, agent.id, draft.revision


async def test_an_endpoint_with_room_publishes() -> None:
    catalog, workspace_id, actor, agent_id, revision = await publishing_against(
        endpoint_with(128_000, 4_096)
    )

    published = await catalog.publish(workspace_id, actor, agent_id, revision, "req-3")

    assert published.version.version_number == 1


async def test_targets_that_do_not_fit_are_refused_with_per_segment_advice() -> None:
    catalog, workspace_id, actor, agent_id, revision = await publishing_against(
        endpoint_with(8_192, 4_096)
    )

    with pytest.raises(ContextBudgetUnsatisfied) as refused:
        await catalog.publish(workspace_id, actor, agent_id, revision, "req-3")

    advice = {entry.segment: entry.suggested for entry in refused.value.fit.advice}
    assert SegmentName.TOOL_SCHEMAS in advice
    assert refused.value.fit.allowance == 4_096


async def test_the_advice_does_not_apply_itself() -> None:
    """The whole point of returning it rather than acting on it: the author's
    draft is exactly what they wrote, and a second publish is refused the same
    way until they change it."""
    catalog, workspace_id, actor, agent_id, revision = await publishing_against(
        endpoint_with(8_192, 4_096)
    )
    with pytest.raises(ContextBudgetUnsatisfied):
        await catalog.publish(workspace_id, actor, agent_id, revision, "req-3")

    draft = await catalog.get_draft(workspace_id, actor, agent_id, "req-5")

    assert draft.spec.context_budget is None
    with pytest.raises(ContextBudgetUnsatisfied):
        await catalog.publish(workspace_id, actor, agent_id, revision, "req-4")


async def test_an_author_who_takes_the_advice_can_publish() -> None:
    endpoint = endpoint_with(8_192, 4_096)
    fit = fit_budget(
        ContextWindow(8_192, reserved_output_tokens=4_096), ContextBudget().resolve()
    )
    catalog, workspace_id, actor, agent_id, revision = await publishing_against(
        endpoint,
        {
            "segments": [
                {"segment": entry.segment.value, "target_tokens": entry.suggested}
                for entry in fit.advice
            ]
        },
    )

    published = await catalog.publish(workspace_id, actor, agent_id, revision, "req-3")

    assert published.version.version_number == 1


async def test_a_window_that_cannot_hold_the_minimum_fails_at_publish() -> None:
    """§7.4.2: the static configuration fails here, and its runtime twin is
    `paused(context_overflow)`."""
    catalog, workspace_id, actor, agent_id, revision = await publishing_against(
        endpoint_with(1_000, 900)
    )

    with pytest.raises(ContextWindowTooSmall):
        await catalog.publish(workspace_id, actor, agent_id, revision, "req-3")
