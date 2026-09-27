"""备用端点：写在模型策略里，发布时校验（§7.4.1，v2.12）。

主端点连不上时，一个只写了一个 `endpoint_id` 的 Agent 就只能让这一轮失败。
备用端点是有序的，最多两个——再多只会让一轮在一串都连不上的端点上排队等超时。

不写就不带这个键：已发布版本的内容哈希不得变化。
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError
from tiny_hermes.agents.application.service import (
    ContextBudgetUnsatisfied,
    ModelEndpointUnavailable,
)
from tiny_hermes.agents.domain.models import (
    AgentSpec,
    EndpointModelPolicy,
    normalize_agent_spec,
)
from tiny_hermes.model_catalog.domain.models import EndpointStatus

from .test_agent_models import valid_spec
from .test_summary_endpoint import _endpoint, _Publisher, publisher  # noqa: F401


def _policy(main: str, *fallbacks: str, summary: str | None = None) -> dict[str, object]:
    policy: dict[str, object] = {
        "provider": "openai_compatible",
        "endpoint_id": main,
        "fallback_endpoint_ids": list(fallbacks),
    }
    if summary is not None:
        policy["summary_endpoint_id"] = summary
    return policy


def test_two_fallbacks_are_accepted_in_order() -> None:
    first, second = str(uuid4()), str(uuid4())
    policy = EndpointModelPolicy.model_validate(_policy(str(uuid4()), first, second))

    assert [str(item) for item in policy.fallback_endpoint_ids] == [first, second]


def test_a_third_fallback_is_refused() -> None:
    with pytest.raises(ValidationError):
        EndpointModelPolicy.model_validate(
            _policy(str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4()))
        )


def test_a_fallback_repeating_the_main_endpoint_is_refused() -> None:
    main = str(uuid4())
    with pytest.raises(ValidationError):
        EndpointModelPolicy.model_validate(_policy(main, main))


def test_a_fallback_named_twice_is_refused() -> None:
    other = str(uuid4())
    with pytest.raises(ValidationError):
        EndpointModelPolicy.model_validate(_policy(str(uuid4()), other, other))


def test_no_fallback_carries_no_key_so_old_versions_hash_the_same() -> None:
    main = str(uuid4())
    without = AgentSpec.model_validate(
        {**valid_spec(), "model_policy": {"provider": "openai_compatible", "endpoint_id": main}}
    )
    empty = AgentSpec.model_validate({**valid_spec(), "model_policy": _policy(main)})

    document, digest = normalize_agent_spec(without)
    assert "fallback_endpoint_ids" not in document["model_policy"]  # type: ignore[operator]
    assert normalize_agent_spec(empty)[1] == digest


async def test_a_published_fallback_is_kept(publisher: _Publisher) -> None:  # noqa: F811
    main, backup = _endpoint(128_000), _endpoint(64_000)

    version = await publisher.publish(
        {**valid_spec(), "model_policy": _policy(str(main.id), str(backup.id))}
    )

    policy = AgentSpec.model_validate(version.spec).model_policy
    assert isinstance(policy, EndpointModelPolicy)
    assert policy.fallback_endpoint_ids == (backup.id,)


async def test_a_disabled_fallback_is_refused_at_publish(
    publisher: _Publisher,  # noqa: F811
) -> None:
    main = _endpoint(128_000)
    disabled = _endpoint(128_000, status=EndpointStatus.DISABLED)

    with pytest.raises(ModelEndpointUnavailable):
        await publisher.publish(
            {**valid_spec(), "model_policy": _policy(str(main.id), str(disabled.id))}
        )


async def test_a_summary_endpoint_smaller_than_a_fallback_is_refused(
    publisher: _Publisher,  # noqa: F811
) -> None:
    """Once a Run has switched, its conversation is planned against the
    fallback's window, and the summarizer is then asked to read that much."""
    main, summary, backup = _endpoint(64_000), _endpoint(64_000), _endpoint(128_000)

    with pytest.raises(ContextBudgetUnsatisfied):
        await publisher.publish(
            {
                **valid_spec(),
                "model_policy": _policy(str(main.id), str(backup.id), summary=str(summary.id)),
            }
        )
