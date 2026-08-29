"""摘要模型的窗口不得小于主模型的，而且要在发布时就拒绝。

上游 Hermes 把这条记成了它最常见的退化原因：摘要模型的上下文窗口不够时，它的
`summarize` 直接吞掉异常返回 `None`，压缩器就此丢弃中间轮次、不生成任何摘要——
运行时最难被人注意到的那种失败。把这条挪到发布时的静态检查，运行时那一刻就
不会到来。复用 `context_budget_unsatisfied` 那条既有的发布校验路径，不新开一条：
一个 Agent 只应该有一种"发布时窗口不够"的失败形状。
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from tiny_hermes.agents.application.service import AgentCatalog, ContextBudgetUnsatisfied
from tiny_hermes.agents.domain.models import AgentSpec, AgentVersion
from tiny_hermes.agents.infrastructure.memory_store import MemoryAgentStore
from tiny_hermes.model_catalog.domain.models import (
    EndpointStatus,
    ModelEndpoint,
    ModelEndpointSpec,
    UsageQuality,
)
from tiny_hermes.tenancy.domain.models import Actor, Role

from .test_agent_models import valid_spec

#: One shared registry per test, cleared by the autouse fixture below. `_spec`
#: needs somewhere to put the endpoints it invents before `publisher` exists to
#: hold them, and a plain module dict keeps its signature the same as the
#: plan's — `_spec(main_window=..., summary_window=...)`, no store to thread
#: through.
_ENDPOINTS: dict[UUID, ModelEndpoint] = {}


@pytest.fixture(autouse=True)
def reset_endpoints() -> None:
    _ENDPOINTS.clear()


def _endpoint(context_window: int) -> ModelEndpoint:
    now = datetime.now(UTC)
    endpoint = ModelEndpoint(
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
    _ENDPOINTS[endpoint.id] = endpoint
    return endpoint


def _spec(*, main_window: int, summary_window: int | None) -> dict[str, object]:
    main = _endpoint(main_window)
    policy: dict[str, object] = {
        "provider": "openai_compatible",
        "endpoint_id": str(main.id),
    }
    if summary_window is not None:
        summary = _endpoint(summary_window)
        policy["summary_endpoint_id"] = str(summary.id)
    return {**valid_spec(), "model_policy": policy}


@dataclass
class _Endpoints:
    """The smallest store `AgentCatalog` needs, backed by the module registry."""

    async def read(self, endpoint_id: UUID) -> ModelEndpoint | None:
        return _ENDPOINTS.get(endpoint_id)


class _Publisher:
    """Wraps the async `AgentCatalog` so tests read as `publisher.publish(spec)`."""

    def __init__(self) -> None:
        self.workspace_id = uuid4()
        self.actor = Actor(uuid4(), False)
        store = MemoryAgentStore()
        store.roles[(self.workspace_id, self.actor.id)] = Role.DEVELOPER
        self.catalog = AgentCatalog(store, endpoints=_Endpoints())  # type: ignore[arg-type]

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


async def test_a_smaller_summary_endpoint_is_refused_at_publish(publisher: _Publisher) -> None:
    with pytest.raises(ContextBudgetUnsatisfied) as raised:
        await publisher.publish(_spec(main_window=128_000, summary_window=32_000))

    assert "summary" in str(raised.value).lower()


async def test_the_same_size_is_accepted(publisher: _Publisher) -> None:
    await publisher.publish(_spec(main_window=128_000, summary_window=128_000))


async def test_no_summary_endpoint_means_the_agent_s_own(publisher: _Publisher) -> None:
    version = await publisher.publish(_spec(main_window=128_000, summary_window=None))

    spec = AgentSpec.model_validate(version.spec)
    assert spec.model_policy.summary_endpoint_id is None  # type: ignore[union-attr]
