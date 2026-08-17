"""What an Agent says "finished" means, declared at publish.

Design §4.2 and §4.3. The condition is optional because `AgentSpec` is
content-hashed and every published version must keep its hash: a spec that
never heard of completion conditions still normalizes to the same bytes, which
is what lets `schema_version` stay 1 and leaves `agent_versions` unmigrated.
That claim is pinned here against the same literal the model-policy widening
used.

The rest of these are publish-time refusals. A completion condition the
platform could never check would become a Run that loops until it hits the
round ceiling — a runtime mystery for whoever submitted it, caused by something
its author could have been told at publish.
"""

import pytest
from pydantic import ValidationError
from tiny_hermes.agents.domain.models import AgentSpec, normalize_agent_spec

from .test_agent_models import valid_spec
from .test_model_policy import DETERMINISTIC_HASH


def spec_with(completion: object, **overrides: object) -> AgentSpec:
    return AgentSpec.model_validate(
        {**valid_spec(), "tools": ["shell.exec"], **overrides, "completion": completion}
    )


def test_a_spec_that_declares_nothing_hashes_to_what_it_did() -> None:
    """The claim that lets `schema_version` stay 1 and skips the migration."""
    spec = AgentSpec.model_validate(valid_spec())
    document, content_hash = normalize_agent_spec(spec)

    assert content_hash == DETERMINISTIC_HASH
    assert "completion" not in document
    assert spec.completion is None


def test_a_declared_condition_survives_normalization() -> None:
    spec = spec_with(
        {
            "expected_artifacts": ["report.md"],
            "verification_command": "pytest -q",
            "constraints": "Do not touch the database.",
            "stop_conditions": {"max_rounds": 12},
        }
    )

    document, _ = normalize_agent_spec(spec)

    assert document["completion"] == {
        "expected_artifacts": ["report.md"],
        "verification_command": "pytest -q",
        "constraints": "Do not touch the database.",
        "stop_conditions": {"max_rounds": 12},
    }


def test_a_condition_with_nothing_checkable_is_refused() -> None:
    """`goal.py` returns `continue` for a declared goal with no checks.

    A condition that declares only free text would therefore never be met: the
    Run would work until the round ceiling and pause. Refusing it here turns
    that into a message its author reads while writing the Agent.
    """
    with pytest.raises(ValidationError, match="checkable"):
        spec_with({"constraints": "Be quick."})
    with pytest.raises(ValidationError, match="checkable"):
        spec_with({})


def test_a_verification_command_without_the_tool_that_runs_commands_is_refused() -> None:
    """The verification runs down `shell.exec`'s path or it does not run.

    §4.3 reuses that path on purpose, so the host-fallback ban proven in 0.1
    covers the verification unchanged. An Agent that binds no command tool has
    no such path, and a slice for it opens no sandbox at all
    (`worker.py:249`).
    """
    with pytest.raises(ValidationError, match="shell.exec"):
        spec_with({"verification_command": "pytest -q"}, tools=["file.read"])


def test_expected_artifacts_without_a_sandbox_are_refused() -> None:
    """Nothing can write to `/workspace/data` when nothing is bound.

    The artifact could never appear, so the condition could never be met.
    """
    with pytest.raises(ValidationError, match="artifact"):
        spec_with({"expected_artifacts": ["report.md"]}, tools=[])


def test_artifact_paths_follow_the_rule_the_workspace_already_has() -> None:
    """The same normalization the manifest and the file tools use.

    A path declared one way and recorded another would be a check that fails on
    a file that exists.
    """
    for hostile in ("/workspace/data/report.md", "../escape.md", "a\\b.md", "", "."):
        with pytest.raises(ValidationError):
            spec_with({"expected_artifacts": [hostile]})


def test_artifact_paths_are_normalized_and_may_not_repeat() -> None:
    spec = spec_with({"expected_artifacts": ["résumé.md"]})
    assert spec.completion is not None
    assert spec.completion.expected_artifacts == ("résumé.md",)

    with pytest.raises(ValidationError, match="once"):
        spec_with({"expected_artifacts": ["résumé.md", "résumé.md"]})


def test_a_stop_condition_looser_than_the_limit_that_will_stop_it_is_refused() -> None:
    """`max_rounds` above `max_model_calls` promises rounds nobody will get.

    The budget stops the Run first, and the Agent's author would read the
    declared ceiling as the one in force.
    """
    with pytest.raises(ValidationError, match="max_model_calls"):
        spec_with(
            {"verification_command": "pytest -q", "stop_conditions": {"max_rounds": 21}}
        )


def test_a_blank_verification_command_is_not_a_verification_command() -> None:
    with pytest.raises(ValidationError):
        spec_with({"verification_command": "   "})
