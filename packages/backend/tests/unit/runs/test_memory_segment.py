"""The memory segment, and the exit criterion it exists to satisfy.

§7.4.2 gives memory its own segment with priority 3 in the trimming order, and
the roadmap's fourth exit check says: when the segment is over budget, the
low-relevance memories go first and **nothing incompressible is touched**.

Two properties carry that. Memories arrive highest-relevance first, so trimming
from the tail *is* trimming low relevance — the ordering decision is made in the
library and the planner only has to keep it. And every memory is droppable, so
the floor is measured without them: a subject with a large memory loses memories
rather than sending the Run to `paused(context_overflow)` while the platform was
still holding a segment it is allowed to drop.
"""

from typing import Any
from uuid import uuid4

from tiny_hermes.runs.domain.context_budget import (
    DEFAULT_SEGMENTS,
    ContextWindow,
    SegmentBudget,
    SegmentName,
    StoredMessage,
    plan_context,
)
from tiny_hermes.runs.domain.models import CanonicalMessage, TextBlock

RULES = "Follow the platform's rules."
PERSONALITY = "You are concise."


def stored(*messages: CanonicalMessage) -> tuple[StoredMessage, ...]:
    return tuple(
        StoredMessage(id=uuid4(), sequence=index, message=message)
        for index, message in enumerate(messages, start=1)
    )


def says(text: str, role: Any = "user") -> CanonicalMessage:
    return CanonicalMessage(role=role, blocks=(TextBlock(text=text),))


def plan(window: ContextWindow, **extra: Any):
    return plan_context(
        window=window,
        safety_rules=RULES,
        personality=PERSONALITY,
        tool_schemas=(),
        history=stored(says("what should I do about the rollout?")),
        **extra,
    )


def segments_with(memory: SegmentBudget) -> dict[SegmentName, SegmentBudget]:
    return {**DEFAULT_SEGMENTS, SegmentName.MEMORY: memory}


ROOMY = ContextWindow(context_window=1_000_000, reserved_output_tokens=1_000)


def test_memories_that_fit_are_all_sent() -> None:
    result = plan(ROOMY, memories=["I prefer terse answers.", "My timezone is CST."])

    assert result.fits
    assert result.memories == ("I prefer terse answers.", "My timezone is CST.")
    assert not any(item.segment is SegmentName.MEMORY for item in result.trimmed)


def test_the_segment_ceiling_caps_memory_in_a_window_with_all_the_room() -> None:
    """"This segment was never allowed to be this big" is true of a round with
    unlimited space, so it is answered before the window is looked at once."""
    result = plan(
        ROOMY,
        memories=["x" * 400, "y" * 400, "z" * 400],
        segments=segments_with(SegmentBudget(0, 40, 40, trimmable=True, priority=3)),
    )

    assert result.fits
    assert len(result.memories) < 3
    record = next(item for item in result.trimmed if item.segment is SegmentName.MEMORY)
    assert record.dropped >= 1


def test_the_tail_goes_first_because_the_tail_is_the_least_relevant() -> None:
    """The library hands them over ranked, so trimming from the end is what
    "低相关记忆先移除" means in code."""
    result = plan(
        ROOMY,
        memories=["most relevant", "middling", "least relevant"],
        segments=segments_with(SegmentBudget(0, 14, 14, trimmable=True, priority=3)),
    )

    assert result.memories
    assert result.memories[0] == "most relevant"
    assert "least relevant" not in result.memories


def test_a_memory_is_dropped_whole_and_never_truncated() -> None:
    """Half a remembered sentence is a claim nobody made."""
    kept = "I always want the summary before the detail."
    result = plan(
        ROOMY,
        memories=[kept, "y" * 600],
        segments=segments_with(SegmentBudget(0, 30, 30, trimmable=True, priority=3)),
    )

    assert all(item in (kept, "y" * 600) for item in result.memories)


def test_a_large_memory_loses_memories_rather_than_overflowing_the_run() -> None:
    """The exit criterion. Memory is trimmable, so it comes out of the floor:
    a Run must not pause for context overflow while the platform is still
    holding a segment it is allowed to drop."""
    tight = ContextWindow(context_window=700, reserved_output_tokens=200)

    result = plan(tight, memories=["m" * 500, "n" * 500, "o" * 500])

    assert result.fits
    assert len(result.memories) < 3


def test_trimming_memory_never_touches_the_incompressible_segments() -> None:
    """Safety rules and personality are 不可裁剪内容. A memory trim may not
    reach them, however tight the round is."""
    tight = ContextWindow(context_window=700, reserved_output_tokens=200)

    result = plan(tight, memories=["m" * 500, "n" * 500, "o" * 500])

    touched = {item.segment for item in result.trimmed}
    assert SegmentName.SAFETY_RULES not in touched
    assert SegmentName.PERSONALITY not in touched


def test_a_run_with_no_memories_plans_exactly_as_it_did_before() -> None:
    """The segment is allocated for every Agent; an Agent with nothing
    remembered pays nothing for it and records no trim."""
    result = plan(ROOMY)

    assert result.memories == ()
    assert not any(item.segment is SegmentName.MEMORY for item in result.trimmed)
