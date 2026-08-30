"""Where a reused summary is allowed to stand, and what happens when it cannot.

`test_compaction_summary.py::test_a_summary_that_cuts_deeper_than_it_covers_falls_back`
proves the wiring end to end — the Worker really does fall back through a real
HTTP round. What it cannot prove on its own is *why* the candidate was
rejected: a `source == "structural"` result looks identical however it came
about. This file calls `plan_context` and `_honestly_widens` directly and
asserts on the `ContextPlan` in hand instead of on a persisted event.

`plan_context` no longer searches for a boundary when it is given a
`CoveredSummary`: it pins the boundary to the range that summary explains, so
a text that would once have bought itself room by cutting one message deeper
than it covers now simply does not fit, and the whole reuse is refused. That
is the trade this file pins: the summary is used at its own range or not at
all, never at somebody else's. `_honestly_widens`'s coverage comparison
survives as corroboration of the pin, not as the thing preventing the widening
— and the tests below say which of the two actually fired.

The seed shape here (`pairs=8, size=3_000` old turns, `SAFETY_PREAMBLE` and
"You are concise." as the fixed segments) is the exact shape
`test_compaction_summary.py`'s "cuts deeper" and cost-ceiling tests calibrate
against — this file is where that calibration was actually worked out, moved
here per code review rather than left as a comment claiming a robustness the
integration test could not prove.
"""

from uuid import uuid4

# `_honestly_widens` is private and asserted on directly, which pyright
# flags — deliberately, per code review: it is the only way to check which
# of its two rejection branches fired on a `ContextPlan`, rather than
# inferring it from a persisted `source` string that looks the same either
# way.
from tiny_hermes.runs.application.worker import (
    _honestly_widens,  # pyright: ignore[reportPrivateUsage]
)
from tiny_hermes.runs.domain.context_budget import (
    ContextWindow,
    CoveredSummary,
    plan_context,
)
from tiny_hermes.runs.domain.models import (
    SAFETY_PREAMBLE,
    CanonicalMessage,
    StoredMessage,
    TextBlock,
)

#: The endpoint `test_compaction_summary.py` builds its `agent_on_the_small_
#: endpoint` fixture against (`SMALL_ENDPOINT` in `test_context_budget.py`).
_WINDOW = ContextWindow(context_window=13_568, reserved_output_tokens=4_096)
_PERSONALITY = "You are concise."  # `VALID_SPEC["personality"]`, matched exactly


def _seeded_history(pairs: int, size: int) -> tuple[StoredMessage, ...]:
    """`pairs` old user/assistant exchanges of `size` ASCII characters each,
    then one short new question — the same shape
    `test_compaction_summary.py::_seed_old_turns` writes into
    `session_messages`, built here with no database at all."""
    history: list[StoredMessage] = []
    sequence = 0
    for _ in range(pairs):
        for role, filler in (("user", "u"), ("assistant", "a")):
            sequence += 1
            history.append(
                StoredMessage(
                    id=uuid4(),
                    sequence=sequence,
                    message=CanonicalMessage(
                        role=role,  # pyright: ignore[reportArgumentType]
                        blocks=(TextBlock(text=filler * size),),
                    ),
                )
            )
    sequence += 1
    history.append(
        StoredMessage(
            id=uuid4(),
            sequence=sequence,
            message=CanonicalMessage(
                role="user", blocks=(TextBlock(text="and what is left?"),)
            ),
        )
    )
    return tuple(history)


def _plan(
    history: tuple[StoredMessage, ...],
    stored_summary: str | None = None,
    covers: int = 10,
):
    """`covers` defaults to this seed shape's own structural boundary — the
    range the Worker would have asked a summarizer to explain here."""
    return plan_context(
        window=_WINDOW,
        safety_rules=SAFETY_PREAMBLE,
        personality=_PERSONALITY,
        tool_schemas=(),
        history=history,
        stored_summary=(
            None if stored_summary is None else CoveredSummary(stored_summary, covers)
        ),
    )


def test_the_baseline_structural_boundary_for_this_seed_shape() -> None:
    """Pinned so the two tests below have a number to compare against, and
    so a change to `DEFAULT_SEGMENTS` or this window fails *here*, loudly,
    rather than silently shifting what the other two tests actually cover."""
    history = _seeded_history(pairs=8, size=3_000)

    baseline = plan_context(
        window=_WINDOW, safety_rules=SAFETY_PREAMBLE, personality=_PERSONALITY,
        tool_schemas=(), history=history,
    )

    assert baseline.fits
    assert baseline.compacted is not None
    assert baseline.compacted.source == "structural"
    assert baseline.compacted.last_sequence == 10


def test_a_text_too_large_to_fit_is_rejected_on_the_fits_check() -> None:
    """The oversized-text half: the candidate does not fit *at all*, at any
    `through` the search tries — `_honestly_widens` must say no because of
    `plan.fits`, not because of the coverage comparison."""
    history = _seeded_history(pairs=8, size=3_000)
    covered_last = 10

    candidate = _plan(history, stored_summary="超长摘要片段" * 20_000)

    assert candidate.fits is False
    assert _honestly_widens(candidate, covered_last) is False


def test_a_text_that_would_have_to_cut_deeper_is_not_given_the_chance() -> None:
    """The case `test_compaction_summary.py`'s "cuts deeper" test drives
    through a real Run: a summary text bigger than the terse structural one
    (§7.4.2's seven sections cost more than a one-sentence count) but not so
    big it fails outright. The `through` search used to settle one message
    past `covered_last` to make it fit, producing a `CompactionRecord` whose
    range claimed a turn the summarizer never read.

    Pinning removes the move rather than catching it after the fact: at its
    own boundary this text does not fit, so nothing is compacted with it and
    `_honestly_widens` refuses on `plan.fits`. Asserted here explicitly —
    `last_sequence == 11` used to be the observable symptom, and this is the
    test that would notice it coming back.
    """
    history = _seeded_history(pairs=8, size=3_000)
    covered_last = 10

    candidate = _plan(history, stored_summary="占位摘要，故意写得比结构摘要长很多。" * 150)

    assert candidate.fits is False
    assert candidate.compacted is None
    assert _honestly_widens(candidate, covered_last) is False


def test_a_text_that_fits_its_own_range_is_accepted() -> None:
    """The contrast case: a short reused summary fits at the boundary it was
    written about, stands in for exactly that range, and `_honestly_widens`
    accepts it — this is the shape
    `test_the_summary_is_generated_once_and_then_reused`'s second round and
    `test_a_reused_summary_brings_a_run_back_under_the_cost_ceiling` both
    rely on.

    Exactly, not at most: a short text would once have settled wherever the
    search first found room, which for a summary covering more than this
    round needs meant the model reading it *and* the turns it describes."""
    history = _seeded_history(pairs=8, size=3_000)
    covered_last = 10

    candidate = _plan(history, stored_summary="已处理，无新增。")

    assert candidate.fits is True
    assert candidate.compacted is not None
    assert candidate.compacted.last_sequence == covered_last
    assert _honestly_widens(candidate, covered_last) is True
