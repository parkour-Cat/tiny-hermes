"""The model policy becomes a choice, and every existing choice keeps its hash.

Widening a schema is safe only if it really is a widening. `schema_version`
stays 1 on the strength of one fact: a spec that validated before still
validates and still normalizes to the same bytes, so no published Agent Version
is disturbed and no row needs migrating. That fact is pinned here as a literal.
"""

from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from tiny_hermes.agents.domain.models import (
    AgentSpec,
    DeterministicModelPolicy,
    EndpointModelPolicy,
    normalize_agent_spec,
)

from .test_agent_models import valid_spec

#: The hash of the canonical deterministic spec before the union existed.
#: If this literal has to change, the change is not a widening.
DETERMINISTIC_HASH = "4fcf412e8da2d827a601dbc4390d072a21bef593c080a2076041edb4ffb6deaf"

ENDPOINT = UUID("11111111-2222-4333-8444-555555555555")


def endpoint_spec(**policy: object) -> AgentSpec:
    fields = {
        **valid_spec(),
        "model_policy": {
            "provider": "openai_compatible",
            "endpoint_id": str(ENDPOINT),
            **policy,
        },
    }
    return AgentSpec.model_validate(fields)


def test_an_existing_deterministic_spec_still_hashes_to_what_it_did() -> None:
    """The claim that lets `schema_version` stay 1."""
    spec = AgentSpec.model_validate(valid_spec())
    _, content_hash = normalize_agent_spec(spec)
    assert content_hash == DETERMINISTIC_HASH
    assert isinstance(spec.model_policy, DeterministicModelPolicy)


def test_an_endpoint_policy_validates_and_normalizes() -> None:
    spec = endpoint_spec()
    assert isinstance(spec.model_policy, EndpointModelPolicy)
    assert spec.model_policy.endpoint_id == ENDPOINT
    document, content_hash = normalize_agent_spec(spec)
    assert document["model_policy"] == {
        "provider": "openai_compatible",
        "endpoint_id": str(ENDPOINT),
        "temperature": None,
        "max_output_tokens": None,
    }
    assert content_hash != DETERMINISTIC_HASH


def test_two_endpoint_policies_that_differ_hash_differently() -> None:
    first = normalize_agent_spec(endpoint_spec(temperature=0.2))[1]
    second = normalize_agent_spec(endpoint_spec(temperature=0.7))[1]
    assert first != second


def test_an_unknown_provider_is_refused_rather_than_falling_back() -> None:
    """A typo in `provider` must not quietly select the stand-in.

    Without a discriminator, a policy the platform does not understand would be
    an Agent that silently answers from a `match` statement while its author
    believes it is talking to a model.
    """
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(
            {**valid_spec(), "model_policy": {"provider": "anthropic_messages"}}
        )


def test_an_endpoint_policy_without_an_endpoint_is_refused() -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(
            {**valid_spec(), "model_policy": {"provider": "openai_compatible"}}
        )


@pytest.mark.parametrize("temperature", [-0.1, 2.1])
def test_a_temperature_outside_the_range_is_refused(temperature: float) -> None:
    with pytest.raises(ValidationError):
        endpoint_spec(temperature=temperature)


@pytest.mark.parametrize("value", [0, -1])
def test_a_useless_output_limit_is_refused(value: int) -> None:
    with pytest.raises(ValidationError):
        endpoint_spec(max_output_tokens=value)


def test_the_deterministic_policy_is_unchanged() -> None:
    """The stand-in did not move. Only what stands beside it is new."""
    policy = DeterministicModelPolicy()
    assert policy.provider == "deterministic"
    assert policy.scenario == "complete"


def test_an_endpoint_policy_forbids_a_stray_field() -> None:
    with pytest.raises(ValidationError):
        endpoint_spec(scenario="complete")


def test_the_endpoint_id_must_be_a_uuid() -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(
            {
                **valid_spec(),
                "model_policy": {
                    "provider": "openai_compatible",
                    "endpoint_id": "acme-gpt",
                },
            }
        )
    # And a real one round-trips through normalization as a string.
    document, _ = normalize_agent_spec(endpoint_spec())
    policy: Any = document["model_policy"]
    assert isinstance(policy, dict)
    fields = cast(dict[str, Any], policy)
    assert fields["endpoint_id"] == str(ENDPOINT)
    assert UUID(str(fields["endpoint_id"])) == ENDPOINT


def test_a_fresh_uuid_makes_a_different_agent() -> None:
    other = uuid4()
    assert (
        normalize_agent_spec(
            AgentSpec.model_validate(
                {
                    **valid_spec(),
                    "model_policy": {
                        "provider": "openai_compatible",
                        "endpoint_id": str(other),
                    },
                }
            )
        )[1]
        != normalize_agent_spec(endpoint_spec())[1]
    )
