"""When a freshly generated model summary may replace the structural plan.

§7.4.2 v2.10: a model summary is accepted only if the plan built on it — as
the new checkpoint — fits, stands on exactly the range it was asked about,
compacts nothing further, and sends less than the round would have sent
without compacting. Only an accepted summary is saved, because a stored
summary is what every later round builds on.

The Worker's end-to-end half is in `test_compaction_summary.py`; this file
calls `plan_context` and `_summary_holds` directly, so each rejection can be
attributed to the rule that made it.
"""

from uuid import uuid4

# `_summary_holds` is private and asserted on directly — the only way to see
# which rule refused a candidate, rather than inferring it from a persisted
# `source` string that looks the same either way.
from tiny_hermes.runs.application.worker import (
    _summary_holds,  # pyright: ignore[reportPrivateUsage]
)
from tiny_hermes.runs.domain.context_budget import (
    ContextPlan,
    ContextWindow,
    CoveredSummary,
    plan_context,
)
from tiny_hermes.runs.domain.models import (
    CanonicalMessage,
    StoredMessage,
    TextBlock,
    safety_preamble,
)

#: The endpoint `test_compaction_summary.py` builds its `agent_on_the_small_
#: endpoint` fixture against (`SMALL_ENDPOINT` in `test_context_budget.py`).
_WINDOW = ContextWindow(context_window=13_568, reserved_output_tokens=4_096)
_PERSONALITY = "You are concise."  # `VALID_SPEC["personality"]`, matched exactly


def _seeded_history(pairs: int, size: int) -> tuple[StoredMessage, ...]:
    """`pairs` old user/assistant exchanges of `size` ASCII characters each,
    then one short new question — the shape
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
                        role=role,  # type: ignore[arg-type]
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
    history: tuple[StoredMessage, ...], summary: CoveredSummary | None = None
) -> ContextPlan:
    return plan_context(
        window=_WINDOW,
        safety_rules=safety_preamble(tools=False),
        personality=_PERSONALITY,
        tool_schemas=(),
        history=history,
        stored_summary=summary,
    )


def _baseline() -> tuple[tuple[StoredMessage, ...], ContextPlan]:
    history = _seeded_history(pairs=8, size=3_000)
    baseline = _plan(history)
    assert baseline.compacted is not None
    assert baseline.compacted.source == "structural"
    assert baseline.before_compaction_estimate is not None
    return history, baseline


def test_a_short_summary_standing_on_its_own_range_is_accepted() -> None:
    history, baseline = _baseline()
    assert baseline.compacted is not None

    candidate = _plan(
        history, CoveredSummary("已处理，无新增。", baseline.compacted.last_sequence)
    )

    assert candidate.checkpoint == baseline.compacted.last_sequence
    assert candidate.compacted is None
    assert _summary_holds(candidate, baseline) is True


def test_a_text_too_large_to_fit_is_refused() -> None:
    history, baseline = _baseline()
    assert baseline.compacted is not None

    candidate = _plan(
        history, CoveredSummary("超长摘要片段" * 20_000, baseline.compacted.last_sequence)
    )

    assert candidate.fits is False
    assert _summary_holds(candidate, baseline) is False


def test_a_text_that_saves_nothing_is_refused() -> None:
    """Fits, stands on its range — and is no shorter than what it replaced.
    The first `/compact` in production applied exactly this and reported
    「已压缩」."""
    history, baseline = _baseline()
    assert baseline.compacted is not None
    assert baseline.before_compaction_estimate is not None
    covered = [
        item for item in history if item.sequence <= baseline.compacted.last_sequence
    ]
    as_long = "".join(item.message.text for item in covered)

    candidate = _plan(history, CoveredSummary(as_long, baseline.compacted.last_sequence))

    assert candidate.input_estimate >= baseline.before_compaction_estimate
    assert _summary_holds(candidate, baseline) is False


def test_a_summary_that_does_not_become_the_checkpoint_is_refused() -> None:
    """Asked about one range, planned at another: whatever the text says, it
    is not the checkpoint it would be saved as."""
    history, baseline = _baseline()

    candidate = _plan(history, CoveredSummary("已处理，无新增。", last_sequence=999))

    assert candidate.checkpoint is None
    assert _summary_holds(candidate, baseline) is False
