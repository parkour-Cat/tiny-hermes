"""装得下也压——到达比例就动。

原来的判据是「装不下」，于是一段离窗口还很远的长会话永远不会被压缩。2026-08-26
那条 80 条消息的会话就是这样：128k 的窗口，压缩一次都没跑过。
"""

from typing import Any
from uuid import uuid4

import pytest
from tiny_hermes.runs.domain.context_budget import ContextPlan, ContextWindow, plan_context
from tiny_hermes.runs.domain.models import CanonicalMessage, StoredMessage, TextBlock

RULES = "Stay inside the platform."
PERSONALITY = "You are a careful assistant."

#: Sized so the fixture below sits at roughly two thirds of the allowance:
#: comfortably under 1.0 (it already fits, the old criterion), and over 0.5
#: (the new default) — the one window that can tell 0.50 and 0.99 apart.
WINDOW = ContextWindow(1_200, reserved_output_tokens=200)


def _stored(*messages: CanonicalMessage) -> tuple[StoredMessage, ...]:
    return tuple(
        StoredMessage(id=uuid4(), sequence=index, message=message)
        for index, message in enumerate(messages, start=1)
    )


def _says(text: str, role: Any = "user") -> CanonicalMessage:
    return CanonicalMessage(role=role, blocks=(TextBlock(text=text),))


@pytest.fixture
def mid_history() -> tuple[StoredMessage, ...]:
    """Fits `WINDOW` outright (the old criterion never fires here) but spends
    about two thirds of the allowance — past a 0.50 ratio, short of a 0.99 one."""
    return _stored(
        _says("the task, stated at some length: " + "t" * 550),
        *(_says(f"round {index}: " + "w" * 350, role="assistant") for index in range(3)),
        _says("what is left?"),
    )


@pytest.fixture
def short_history() -> tuple[StoredMessage, ...]:
    """Nothing here is outside `PROTECTED_RECENT_MESSAGES` plus the current
    request: two messages total, the last of which (the only ``user`` turn)
    *is* the current request. `compactable` cannot reach 2 no matter how the
    window is sized, so structural compaction is not geometrically possible —
    the case `can_compact` exists to recognize."""
    return _stored(_says("hello"), _says("hi there", role="assistant"))


def _plan_with(history: tuple[StoredMessage, ...], *, threshold: float) -> ContextPlan:
    return plan_context(
        window=WINDOW,
        safety_rules=RULES,
        personality=PERSONALITY,
        tool_schemas=(),
        history=history,
        threshold=threshold,
    )


def _last_text(messages: tuple[CanonicalMessage, ...]) -> str:
    block = messages[-1].blocks[-1]
    assert isinstance(block, TextBlock)
    return block.text


def test_a_history_that_fits_is_still_compacted_past_the_threshold(
    mid_history: tuple[StoredMessage, ...],
) -> None:
    plan = _plan_with(mid_history, threshold=0.50)

    assert plan.fits
    assert plan.compacted is not None


def test_below_the_threshold_nothing_is_compacted(
    mid_history: tuple[StoredMessage, ...],
) -> None:
    plan = _plan_with(mid_history, threshold=0.99)

    assert plan.compacted is None


def test_the_current_request_is_never_compacted_away(
    mid_history: tuple[StoredMessage, ...],
) -> None:
    plan = _plan_with(mid_history, threshold=0.01)

    assert _last_text(plan.messages) == _last_text(
        tuple(stored.message for stored in mid_history)
    )


def test_nothing_to_compact_survives_an_aggressive_threshold(
    short_history: tuple[StoredMessage, ...],
) -> None:
    """Pins the `can_compact` gate directly, rather than relying on
    `test_memory_segment.py` / `test_skill_summaries.py` to catch its
    regression by accident (both fail for a reason incidental to this file —
    a squeezed segment that already fit `allowance` but not a
    threshold-scaled target — and either could be renamed or restructured
    without anyone noticing the gate went with it).

    A history with nothing outside the protected tail gives step four's
    search no `through` to pick (`range(2, max(compactable, 0) + 1)` is
    empty). Without `can_compact`, an aggressive threshold would still force
    entry past the trim steps' early returns, land in that empty search, and
    fall out the bottom as `paused(context_overflow)` — discarding a plan
    that already fit for a reason unrelated to how much of it was spent.
    """
    plan = _plan_with(short_history, threshold=0.01)

    assert plan.fits
    assert plan.compacted is None


@pytest.fixture
def costly_to_compact() -> tuple[StoredMessage, ...]:
    """Originals that fit `WINDOW` outright, and whose only compactable
    boundary makes the round *bigger*.

    Two tiny turns, then one long one, then the request. `compactable` is 2,
    so step four's search has exactly one `through` to try — and standing a
    structural summary in for two turns worth 13 tokens costs 88, which pushes
    the round past `allowance`. Before the ratio trigger this shape was
    unreachable: the cascade was only ever entered by a round that already did
    not fit, and one that did not fit had nothing to lose by the search
    failing."""
    return _stored(
        _says("ok"),
        _says("sure", role="assistant"),
        _says("here is the log: " + "h" * 2_440),
        _says("what now?"),
    )


def test_originals_that_fit_are_sent_when_the_search_finds_nothing(
    costly_to_compact: tuple[StoredMessage, ...],
) -> None:
    """`paused(context_overflow)` is for a round that genuinely cannot be
    sent. This one can: the same history at a 0.99 ratio goes out untouched,
    and crossing a 0.50 ratio is not a reason to refuse to send it."""
    unforced = _plan_with(costly_to_compact, threshold=0.99)
    assert unforced.fits
    assert unforced.compacted is None
    assert unforced.input_estimate <= unforced.allowance

    plan = _plan_with(costly_to_compact, threshold=0.50)

    assert plan.compacted is None
    assert plan.fits
    assert plan.input_estimate == unforced.input_estimate
    assert plan.messages == tuple(stored.message for stored in costly_to_compact)
