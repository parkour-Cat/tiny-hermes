"""The post-run review's prompt and reply (§15.4, v2.12).

The reply is one JSON object, and anything else is unreadable rather than
guessed at: a proposal is something a person will spend time reviewing, and
one assembled from a half-understood answer is worse than none.
"""

import pytest
from pydantic import ValidationError
from tiny_hermes.agents.domain.models import AgentSpec, SkillReview, normalize_agent_spec
from tiny_hermes.runs.domain.skill_review import (
    REVIEW_MARKER,
    ReviewUnreadable,
    parse_review,
    review_prompt,
)

from ..agents.test_agent_models import valid_spec

SKILL_MD = "---\nname: rollout\ndescription: Drain a machine.\n---\n\n# Rollout\n"


def test_the_prompt_carries_the_transcript_and_the_bound_skills() -> None:
    prompt = review_prompt("user: take web-3 out", [("rollout", "Drain a machine.")])

    assert REVIEW_MARKER in prompt
    assert "user: take web-3 out" in prompt
    assert "rollout: Drain a machine." in prompt


def test_nothing_worth_keeping_is_an_answer() -> None:
    assert parse_review('{"decision": "none"}').decision == "none"


def test_a_new_skill_carries_its_whole_skill_md() -> None:
    parsed = parse_review('{"decision": "new", "skill_md": ' + _json(SKILL_MD) + "}")

    assert parsed.decision == "new"
    assert parsed.skill_md == SKILL_MD
    assert parsed.skill is None


def test_a_patch_names_the_skill_it_changes() -> None:
    parsed = parse_review(
        '{"decision": "patch", "skill": "rollout", "skill_md": ' + _json(SKILL_MD) + "}"
    )

    assert (parsed.decision, parsed.skill) == ("patch", "rollout")


def test_a_fenced_reply_is_read() -> None:
    """Models wrap JSON in a code fence often enough that refusing it would
    waste most reviews; the fence is the only thing tolerated."""
    assert parse_review('```json\n{"decision": "none"}\n```').decision == "none"


@pytest.mark.parametrize(
    "reply",
    [
        "I think this could be a skill.",
        '{"decision": "maybe"}',
        '{"decision": "new"}',
        '{"decision": "patch", "skill_md": "x"}',
        '{"decision": "new", "skill_md": ""}',
        "[]",
    ],
)
def test_anything_else_is_unreadable(reply: str) -> None:
    with pytest.raises(ReviewUnreadable):
        parse_review(reply)


def test_the_setting_defaults_to_ten_tool_calls() -> None:
    assert SkillReview().min_tool_calls == 10
    assert SkillReview().enabled is True
    with pytest.raises(ValidationError):
        SkillReview(min_tool_calls=0)


def test_an_agent_that_never_asked_carries_no_key() -> None:
    document, _ = normalize_agent_spec(AgentSpec.model_validate(valid_spec()))
    assert "skill_review" not in document

    asked = AgentSpec.model_validate({**valid_spec(), "skill_review": {"min_tool_calls": 5}})
    assert asked.skill_review == SkillReview(min_tool_calls=5)


def _json(text: str) -> str:
    import json

    return json.dumps(text)


def test_a_review_that_would_pass_a_ceiling_is_not_made() -> None:
    """The review is a real call, so it answers to the same valves a round
    does. A completed Run always has a call left under the round rules, so
    the call counter is checked here, where it can be set up directly."""
    from dataclasses import replace

    from tiny_hermes.runs.application.worker import (
        _side_call_allowed,  # pyright: ignore[reportPrivateUsage]
    )

    from .test_skill_load_limits import context

    roomy = context()
    full = replace(roomy, budget=replace(roomy.budget, consumed_model_calls=20))

    assert _side_call_allowed(roomy, 1_000) is True
    assert _side_call_allowed(full, 1_000) is False
