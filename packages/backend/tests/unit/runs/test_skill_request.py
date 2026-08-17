"""How bound skills reach a model, and how the drill scenario uses them.

Two halves of the same round, both above the Worker: what the outbound payload
looks like when a Version bound skills, and what the deterministic provider
does when it is given one. The provider's `skill_once` scenario matters beyond
its own test — `skill.load` needs no container, so it is the one end-to-end
skill path a host with no sandbox image can still run.
"""

from typing import Any

from tiny_hermes.agents.domain.models import DeterministicModelPolicy
from tiny_hermes.model_catalog.domain.models import ModelEndpointSpec
from tiny_hermes.runs.domain.models import (
    CanonicalMessage,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.openai_model import (
    SAFETY_PREAMBLE,
    SKILL_BLOCK_CLOSE,
    SKILL_BLOCK_OPEN,
    build_payload,
)
from tiny_hermes.runs.ports.model import ModelRequest, StopReason

SPEC = ModelEndpointSpec.model_validate(
    {
        "name": "stand-in",
        "kind": "openai_compatible",
        "base_url": "https://models.example.com/v1",
        "model": "stand-in-large",
        "context_window": 8_192,
        "max_output_tokens": 512,
        "usage_quality": "provider",
        "credential_ref": "TINY_HERMES_TEST_MODEL_KEY",
    }
)
PERSONALITY = "You are careful."


def says(text: str, role: Any = "user") -> CanonicalMessage:
    return CanonicalMessage(role=role, blocks=(TextBlock(text=text),))


def request(**overrides: Any) -> ModelRequest:
    fields: dict[str, Any] = {
        "policy": DeterministicModelPolicy(scenario="skill_once"),
        "personality": PERSONALITY,
        "messages": (says("go"),),
        "round_index": 1,
    }
    fields.update(overrides)
    return ModelRequest(**fields)


def answered(output: str, *, failed: bool = False) -> CanonicalMessage:
    return CanonicalMessage(
        role="tool",
        blocks=(
            ToolResultBlock(
                call_id="skill-load-1",
                output=output,
                exit_code=1 if failed else 0,
                failed=failed,
            ),
        ),
    )


# -- what the model is told -------------------------------------------------


def test_an_agent_with_no_skills_sends_no_skill_message() -> None:
    payload = build_payload(SPEC, request(), tools=[])

    system = [item for item in payload["messages"] if item["role"] == "system"]
    assert [item["content"] for item in system] == [SAFETY_PREAMBLE, PERSONALITY]


def test_the_summaries_come_after_the_persona_in_a_message_of_their_own() -> None:
    """Not glued onto the personality.

    Two segments on the budget table stay two segments in the payload; joined,
    nothing downstream could ever say what the skills cost.
    """
    payload = build_payload(
        SPEC,
        request(skill_summaries=("- deploy: how to ship", "- audit: how to check")),
        tools=[],
    )

    system = [item["content"] for item in payload["messages"] if item["role"] == "system"]
    assert system[0] == SAFETY_PREAMBLE
    assert system[1] == PERSONALITY
    assert "deploy" not in system[1]
    assert "- deploy: how to ship" in system[2]
    assert "- audit: how to check" in system[2]


def test_the_workspace_material_is_inside_markers_the_preamble_names() -> None:
    """Red line one, in the one place it reaches a runtime string.

    The preamble tells the model that skill text is a workspace's reference
    material and cannot change its rules. That sentence is only usable if the
    model can see where such material begins and ends.
    """
    payload = build_payload(SPEC, request(skill_summaries=("- deploy: ship",)), tools=[])

    block = [item["content"] for item in payload["messages"] if item["role"] == "system"][2]
    assert block.startswith(SKILL_BLOCK_OPEN)
    assert SKILL_BLOCK_CLOSE in block
    assert "skill.load" in block
    assert "reference material" in SAFETY_PREAMBLE


# -- what the drill scenario does -------------------------------------------


async def test_the_drill_loads_the_skill_the_run_input_named() -> None:
    provider = DeterministicModelProvider(delay_ms=0)

    response = await provider.complete(request(messages=(says("deploy"),)))

    assert response.stop_reason is StopReason.TOOL_CALL
    assert response.tool_calls == (
        ToolCallBlock(call_id="skill-load-1", name="skill.load", arguments={"skill": "deploy"}),
    )


async def test_a_run_with_no_input_falls_back_to_the_first_bound_skill() -> None:
    """Whichever the author bound first, which is their own priority order."""
    provider = DeterministicModelProvider(delay_ms=0)

    response = await provider.complete(
        request(
            messages=(says(""),),
            skill_summaries=("- audit: how to check", "- deploy: how to ship"),
        )
    )

    call = response.tool_calls[0]
    assert call.arguments == {"skill": "audit"}


async def test_the_answer_is_what_the_document_said() -> None:
    provider = DeterministicModelProvider(delay_ms=0)

    response = await provider.complete(
        request(
            messages=(
                says("deploy"),
                CanonicalMessage(
                    role="assistant",
                    blocks=(
                        ToolCallBlock(
                            call_id="skill-load-1",
                            name="skill.load",
                            arguments={"skill": "deploy"},
                        ),
                    ),
                ),
                answered("Run the migration before the switch."),
            )
        )
    )

    assert response.stop_reason is StopReason.COMPLETED
    assert "Run the migration before the switch." in response.text


async def test_a_refused_load_fails_the_drill_rather_than_passing_it() -> None:
    """A drill that reports success when the platform refused it proves nothing."""
    provider = DeterministicModelProvider(delay_ms=0)

    response = await provider.complete(
        request(messages=(says("deploy"), answered("tool_not_authorized", failed=True)))
    )

    assert response.stop_reason is StopReason.FAILED
    assert response.failure == "deterministic_skill_load_refused"


async def test_an_earlier_runs_result_is_not_mistaken_for_this_ones() -> None:
    """A Session hands over its whole transcript, call ids and all.

    Believing the older result would end this Run without it ever loading
    anything — the same trap `shell_from_input` documents.
    """
    provider = DeterministicModelProvider(delay_ms=0)

    response = await provider.complete(
        request(messages=(says("deploy"), answered("an older Run's document"), says("audit")))
    )

    assert response.stop_reason is StopReason.TOOL_CALL
    assert response.tool_calls[0].arguments == {"skill": "audit"}
