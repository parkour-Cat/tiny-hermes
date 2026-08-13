import pytest
from pydantic import ValidationError
from tiny_hermes.agents.domain.models import AgentSpec, normalize_agent_spec


def valid_spec() -> dict[str, object]:
    return {
        "schema_version": 1,
        "personality": "You are concise.",
        "model_policy": {"provider": "deterministic", "scenario": "complete"},
        "tools": [],
        "limits": {
            "max_execution_seconds": 900,
            "max_elapsed_seconds": 86400,
            "max_model_calls": 20,
            "max_tool_calls": 50,
            "max_derived_retries": 3,
        },
    }


def test_agent_spec_has_stable_normalized_document_and_hash() -> None:
    first = AgentSpec.model_validate(valid_spec())
    reordered = AgentSpec.model_validate(dict(reversed(list(valid_spec().items()))))

    first_json, first_hash = normalize_agent_spec(first)
    second_json, second_hash = normalize_agent_spec(reordered)

    assert first_json == second_json
    assert first_hash == second_hash
    assert len(first_hash) == 64


def test_phase_two_rejects_tools_and_non_deterministic_provider() -> None:
    # `file.read` was the example here until phase 3C implemented it; the rule
    # under test is "no unimplemented tool", not any one name.
    with pytest.raises(ValidationError):
        AgentSpec.model_validate({**valid_spec(), "tools": ["web.search"]})
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(
            {
                **valid_spec(),
                "model_policy": {"provider": "openai_compatible", "scenario": "complete"},
            }
        )


def test_agent_spec_rejects_unknown_fields_and_unsafe_limits() -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate({**valid_spec(), "unknown": True})
    values = valid_spec()
    values["limits"] = {
        "max_execution_seconds": 901,
        "max_elapsed_seconds": 86400,
        "max_model_calls": 20,
        "max_tool_calls": 50,
        "max_derived_retries": 3,
    }
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(values)
