"""不写就不带那个键——已发布 AgentVersion 的内容哈希不得变化。

`compaction_threshold` is the tenth optional field to widen `AgentSpec`
(`completion`, `context_budget` itself, `network`, `delegation`,
`end_user_access`, `mcp_tools`, `http_tools`, `skills`,
`model_policy.summary_endpoint_id`, and now this one). Every one of them
follows the same promise: a spec that never wrote the field carries no key
for it, so every version published before this field existed still hashes to
what it hashed to then.

The bound check has two layers on purpose (§7.4.2 gives the ratio the same
authority split the segment table has): `ContextBudget`'s own field
validator rules out a ratio that cannot mean anything — zero, negative, more
than the whole window — and is necessary but not sufficient. A value inside
`(0, 1]` can still be outside what this platform's administrator configured,
and that is refused at publish, through the same `context_budget_unsatisfied`
path the segment table's own overage uses rather than a new one.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from tiny_hermes.agents.application.service import AgentCatalog, ContextBudgetUnsatisfied
from tiny_hermes.agents.domain.models import (
    AgentSpec,
    AgentVersion,
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
    MAX_COMPACTION_THRESHOLD,
)
from tiny_hermes.tenancy.domain.models import Actor, Role

from .test_agent_models import valid_spec

# -- the widening: no key written, no key carried -----------------------------


def _spec_without_threshold() -> dict[str, object]:
    # `context_budget` carries a segment override so the key survives
    # normalization at all — an author who set nothing there hashes exactly
    # as `test_a_spec_that_declares_no_budget_hashes_to_what_it_did`
    # (`test_context_budget_spec.py`) already pins; this test is about the
    # narrower key inside that dict, not the dict's own presence.
    return {
        **valid_spec(),
        "context_budget": {"segments": [{"segment": "memory", "target_tokens": 2_048}]},
    }


def _spec_with_threshold(value: float) -> dict[str, object]:
    return {
        **valid_spec(),
        "context_budget": {
            "segments": [{"segment": "memory", "target_tokens": 2_048}],
            "compaction_threshold": value,
        },
    }


def test_a_spec_that_does_not_set_the_threshold_hashes_as_before() -> None:
    spec = AgentSpec.model_validate(_spec_without_threshold())

    document, _ = normalize_agent_spec(spec)

    assert "compaction_threshold" not in document["context_budget"]  # type: ignore[operator]


def test_setting_it_changes_the_hash() -> None:
    without = normalize_agent_spec(AgentSpec.model_validate(_spec_without_threshold()))[1]
    with_threshold = normalize_agent_spec(
        AgentSpec.model_validate(_spec_with_threshold(0.6))
    )[1]

    assert without != with_threshold


# -- the field validator: necessary, not sufficient ---------------------------


def test_a_threshold_above_one_is_refused_by_the_field_validator() -> None:
    with pytest.raises(ValidationError):
        ContextBudget.model_validate({"compaction_threshold": 1.5})


def test_a_threshold_of_zero_is_refused_by_the_field_validator() -> None:
    with pytest.raises(ValidationError):
        ContextBudget.model_validate({"compaction_threshold": 0})


# -- the platform's own hard bounds, checked at publish ------------------------


def _endpoint(context_window: int) -> ModelEndpoint:
    now = datetime.now(UTC)
    return ModelEndpoint(
        id=uuid4(),
        spec=ModelEndpointSpec(
            name="acme-gpt",
            base_url="https://models.example.com/v1",
            model="acme-large",
            context_window=context_window,
            max_output_tokens=4_096,
            usage_quality=UsageQuality.PROVIDER,
            credential_ref="TINY_HERMES_MODEL_KEY_ACME",
        ),
        status=EndpointStatus.ACTIVE,
        created_by=uuid4(),
        created_at=now,
        updated_at=now,
    )


@dataclass
class _Endpoints:
    """The smallest store `AgentCatalog` needs, backed by one fixed endpoint."""

    endpoint: ModelEndpoint

    async def read(self, endpoint_id: UUID) -> ModelEndpoint | None:
        return self.endpoint if endpoint_id == self.endpoint.id else None


class _Publisher:
    """Wraps the async `AgentCatalog` so tests read as `publisher.publish(spec)`.

    Same shape as `test_summary_endpoint.py`'s `_Publisher` — a different
    file, the same "publish is where a static configuration is caught"
    pattern this platform uses for every context-budget refusal.
    """

    def __init__(self) -> None:
        self.workspace_id = uuid4()
        self.actor = Actor(uuid4(), False)
        self.endpoint = _endpoint(128_000)
        store = MemoryAgentStore()
        store.roles[(self.workspace_id, self.actor.id)] = Role.DEVELOPER
        self.catalog = AgentCatalog(store, endpoints=_Endpoints(self.endpoint))  # type: ignore[arg-type]

    async def publish(self, spec: dict[str, object]) -> AgentVersion:
        agent = await self.catalog.create_agent(
            self.workspace_id, self.actor, "Analyst", "analyst", "req-1"
        )
        draft = await self.catalog.replace_draft(
            self.workspace_id, self.actor, agent.id, 1, spec, "req-2"
        )
        result = await self.catalog.publish(
            self.workspace_id, self.actor, agent.id, draft.revision, "req-3"
        )
        return result.version


@pytest.fixture
def publisher() -> _Publisher:
    return _Publisher()


def _spec_with_endpoint_threshold(endpoint: ModelEndpoint, value: float) -> dict[str, object]:
    return {
        **valid_spec(),
        "model_policy": {
            "provider": "openai_compatible",
            "endpoint_id": str(endpoint.id),
        },
        "context_budget": {"compaction_threshold": value},
    }


async def test_a_threshold_outside_the_platform_bounds_is_refused(
    publisher: _Publisher,
) -> None:
    # Inside the field validator's `(0, 1]` — this is the case that validator
    # alone cannot catch, and the reason the publish-time check exists at all.
    above_the_platform_bound = MAX_COMPACTION_THRESHOLD + 0.01
    assert above_the_platform_bound <= 1

    with pytest.raises(ContextBudgetUnsatisfied):
        await publisher.publish(
            _spec_with_endpoint_threshold(publisher.endpoint, above_the_platform_bound)
        )


async def test_a_threshold_inside_the_platform_bounds_publishes(
    publisher: _Publisher,
) -> None:
    version = await publisher.publish(
        _spec_with_endpoint_threshold(publisher.endpoint, MAX_COMPACTION_THRESHOLD)
    )

    assert version.version_number == 1
