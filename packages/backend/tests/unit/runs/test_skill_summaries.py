"""What bound skills cost before anything is loaded, and what gives way first.

Progressive loading (design §10.1) is a budget claim as much as a product one:
a workspace with forty skills puts forty one-line summaries in every request,
and the段 they live in has a ceiling like any other. This file is the planner's
half of it — the two reasons a summary disappears are different reasons and are
tested apart:

* the segment's own `max_tokens`, which is true of a round with all the room in
  the world; and
* step two of the fixed trimming order, which is only about *this* round being
  over the window, and takes no more than the overage.

Neither one ever truncates a summary. Half a summary describes a skill the
model would then load for the wrong reason.
"""

from typing import Any
from uuid import uuid4

from tiny_hermes.runs.domain.context_budget import (
    DEFAULT_SEGMENTS,
    ContextWindow,
    SegmentBudget,
    SegmentName,
    SkillSummary,
    estimate_tokens,
    plan_context,
)
from tiny_hermes.runs.domain.models import (
    CanonicalMessage,
    StoredMessage,
    TextBlock,
    ToolResultBlock,
)

RULES = "Stay inside the platform."
PERSONALITY = "You are a careful assistant."
ROOMY = ContextWindow(context_window=1_000_000, reserved_output_tokens=1_000)


def stored(*messages: CanonicalMessage) -> tuple[StoredMessage, ...]:
    return tuple(
        StoredMessage(id=uuid4(), sequence=index, message=message)
        for index, message in enumerate(messages, start=1)
    )


def says(text: str, role: Any = "user") -> CanonicalMessage:
    return CanonicalMessage(role=role, blocks=(TextBlock(text=text),))


def summary(name: str, *, loaded: bool = False, size: int = 40) -> SkillSummary:
    return SkillSummary(name=name, text=f"- {name}: {'d' * size}", loaded=loaded)


def plan(history: tuple[StoredMessage, ...], window: ContextWindow, **extra: Any):
    return plan_context(
        window=window,
        safety_rules=RULES,
        personality=PERSONALITY,
        tool_schemas=(),
        history=history,
        **extra,
    )


def narrow(max_tokens: int) -> dict[SegmentName, SegmentBudget]:
    """The default table with one row's ceiling moved."""
    table = dict(DEFAULT_SEGMENTS)
    table[SegmentName.SKILL_SUMMARIES] = SegmentBudget(
        0, max_tokens, max_tokens, trimmable=True, priority=2
    )
    return table


def test_summaries_survive_a_round_that_has_room_for_them() -> None:
    summaries = (summary("deploy"), summary("rollback"), summary("audit"))

    planned = plan(stored(says("go")), ROOMY, skill_summaries=summaries)

    assert planned.fits is True
    assert planned.skill_summaries == tuple(item.text for item in summaries)
    assert planned.trimmed == ()


def test_a_summary_costs_the_round_something() -> None:
    """It is a segment on the budget table, so it is measured like one.

    Before this it was allocated and free, which made every number the console
    shows for a skilled Agent wrong by the size of its own summaries.
    """
    bare = plan(stored(says("go")), ROOMY)
    with_skills = plan(stored(says("go")), ROOMY, skill_summaries=(summary("deploy"),))

    assert with_skills.input_estimate > bare.input_estimate


def test_the_segment_ceiling_removes_whole_summaries_never_part_of_one() -> None:
    summaries = (summary("deploy"), summary("rollback"), summary("audit"))
    one = estimate_tokens(summaries[0].text)

    planned = plan(
        stored(says("go")), ROOMY, skill_summaries=summaries, segments=narrow(one * 2)
    )

    assert planned.skill_summaries == (summaries[0].text,)
    assert all(text in {item.text for item in summaries} for text in planned.skill_summaries)


def test_the_ceiling_takes_the_last_binding_first() -> None:
    """The order an author wrote their bindings in is a priority statement."""
    summaries = (summary("deploy"), summary("rollback"), summary("audit"))
    one = estimate_tokens(summaries[0].text)

    planned = plan(
        stored(says("go")),
        ROOMY,
        skill_summaries=summaries,
        segments=narrow(one * 2 + 8),
    )

    record = next(item for item in planned.trimmed if item.segment is SegmentName.SKILL_SUMMARIES)
    assert record.references == ("audit",)
    assert planned.skill_summaries == (summaries[0].text, summaries[1].text)


def test_a_skill_this_run_already_loaded_is_not_a_candidate() -> None:
    """"未命中" is a fact the Run knows, not a guess the platform makes.

    The model called `skill.load` on `audit`, so its text is already in the
    conversation; dropping the line that names it would leave the model holding
    a document it cannot place. `rollback` goes instead, even though the fixed
    order would otherwise have reached it last.
    """
    summaries = (summary("deploy"), summary("rollback"), summary("audit", loaded=True))
    one = estimate_tokens(summaries[0].text)

    planned = plan(
        stored(says("go")),
        ROOMY,
        skill_summaries=summaries,
        segments=narrow(one * 2 + 8),
    )

    record = next(item for item in planned.trimmed if item.segment is SegmentName.SKILL_SUMMARIES)
    assert record.references == ("rollback",)
    assert planned.skill_summaries == (summaries[0].text, summaries[2].text)


def test_every_summary_may_be_loaded_and_then_none_of_them_can_go() -> None:
    """A ceiling is not permission to remove something the Run is using."""
    summaries = (summary("deploy", loaded=True), summary("rollback", loaded=True))

    planned = plan(
        stored(says("go")), ROOMY, skill_summaries=summaries, segments=narrow(1)
    )

    assert planned.skill_summaries == tuple(item.text for item in summaries)
    assert planned.trimmed == ()


def test_the_summaries_go_before_the_recent_history_and_after_tool_results() -> None:
    """Step two of the fixed order, in a round that is genuinely too big.

    The conversation is long enough that trimming the old tool result does not
    save it, so the planner reaches the summaries — and it still gets to keep
    every turn, which is what the order is for.
    """
    history = stored(
        says("start"),
        CanonicalMessage(
            role="tool",
            blocks=(
                ToolResultBlock(
                    call_id="c1", output="x" * 4_000, exit_code=0, failed=False
                ),
            ),
        ),
        says("and now the thing I actually want" + "y" * 200),
    )
    summaries = (summary("deploy", size=400), summary("rollback", size=400))
    tight = ContextWindow(context_window=460, reserved_output_tokens=0)

    planned = plan(history, tight, skill_summaries=summaries)

    assert planned.fits is True
    assert [record.segment for record in planned.trimmed] == [
        SegmentName.OLD_TOOL_RESULTS,
        SegmentName.SKILL_SUMMARIES,
    ]
    assert planned.skill_summaries == (summaries[0].text,)
    assert len(planned.messages) == len(history)


def test_a_round_that_is_barely_over_loses_one_summary_and_not_all_of_them() -> None:
    """Step two asks for the overage, not for the whole segment.

    The distinction matters: the segment already fits its own ceiling by the
    time this runs, so what it is being asked for is room for the conversation.
    """
    summaries = (summary("deploy", size=120), summary("rollback", size=120))
    fixed = plan(stored(says("go")), ROOMY, skill_summaries=summaries).input_estimate
    barely = ContextWindow(
        context_window=fixed - estimate_tokens(summaries[1].text) // 2,
        reserved_output_tokens=0,
    )

    planned = plan(stored(says("go")), barely, skill_summaries=summaries)

    assert planned.fits is True
    assert planned.skill_summaries == (summaries[0].text,)


def test_a_loaded_document_is_a_tool_result_and_is_trimmed_first() -> None:
    """Where the *body* lives needs no new code, and this is the proof.

    A loaded skill comes back as a tool result, so it is in `old_tool_results`
    — the first segment the fixed order touches — and the summary that names it
    outlives it. The model can always ask for the document again; it cannot ask
    for a skill it no longer knows exists.
    """
    body = "The deployment runbook, in full. " * 200
    history = stored(
        says("what does the runbook say"),
        CanonicalMessage(
            role="tool",
            blocks=(
                ToolResultBlock(
                    call_id="skill-load-1", output=body, exit_code=0, failed=False
                ),
            ),
        ),
        says("summarize it"),
    )
    summaries = (summary("deploy", loaded=True),)
    tight = ContextWindow(context_window=300, reserved_output_tokens=0)

    planned = plan(history, tight, skill_summaries=summaries)

    assert planned.fits is True
    first = planned.trimmed[0]
    assert first.segment is SegmentName.OLD_TOOL_RESULTS
    assert first.references == ("skill-load-1",)
    assert planned.skill_summaries == (summaries[0].text,)
    stub = planned.messages[1].blocks[0]
    assert isinstance(stub, ToolResultBlock)
    assert "skill-load-1" in stub.output
    assert body not in stub.output
