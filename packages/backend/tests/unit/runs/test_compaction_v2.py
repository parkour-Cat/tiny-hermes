"""Product design v2.10 §7.4.2: rewrite history rarely, and enough when it is.

The first test here was written before the change it motivates, to turn a
claim read out of the code into a measurement: v2.9.3 took the *smallest*
boundary that fit the whole allowance, so a round just over the ratio trigger
compacted two messages and stayed over it — and the next round was over it
again.
"""

from typing import Any
from uuid import uuid4

from tiny_hermes.runs.domain.context_budget import (
    ContextWindow,
    plan_context,
)
from tiny_hermes.runs.domain.models import (
    CanonicalMessage,
    StoredMessage,
    TextBlock,
)

RULES = "Stay inside the platform."
PERSONALITY = "You are a careful assistant."

#: 100,000 input tokens: small enough to fill with a readable fixture, large
#: enough that the retained tail is its default size rather than a fraction.
WINDOW = ContextWindow(context_window=101_000, reserved_output_tokens=1_000)
THRESHOLD = 0.85


def _stored(*messages: CanonicalMessage) -> tuple[StoredMessage, ...]:
    return tuple(
        StoredMessage(id=uuid4(), sequence=index, message=message)
        for index, message in enumerate(messages, start=1)
    )


def _says(text: str, role: Any = "user") -> CanonicalMessage:
    return CanonicalMessage(role=role, blocks=(TextBlock(text=text),))


def _plan(
    history: tuple[StoredMessage, ...], *, threshold: float = THRESHOLD, **extra: Any
):
    return plan_context(
        window=WINDOW,
        safety_rules=RULES,
        personality=PERSONALITY,
        tool_schemas=(),
        history=history,
        threshold=threshold,
        **extra,
    )


def _conversation(turns: int) -> tuple[StoredMessage, ...]:
    """Alternating user/assistant turns of roughly 1,500 estimated tokens each."""
    messages = [
        _says(f"turn {index} " + "lorem ipsum dolor sit amet " * 160,
              role="user" if index % 2 == 0 else "assistant")
        for index in range(turns)
    ]
    messages.append(_says("what should we do next?"))
    return _stored(*messages)


def _just_over_the_trigger() -> tuple[StoredMessage, ...]:
    """The shortest conversation whose uncompacted estimate crosses the trigger."""
    trigger = WINDOW.input_allowance * THRESHOLD
    for turns in range(2, 400):
        history = _conversation(turns)
        untouched = _plan(history, threshold=1.0)
        if untouched.input_estimate > trigger:
            assert untouched.input_estimate < trigger * 1.05
            return history
    raise AssertionError("fixture never crossed the trigger")


def test_one_compaction_brings_the_round_well_below_the_trigger() -> None:
    history = _just_over_the_trigger()

    result = _plan(history)

    assert result.compacted is not None
    # Not merely "fits": below the trigger with room to grow, or the very next
    # round is over it again and compacts again.
    assert result.input_estimate < WINDOW.input_allowance * THRESHOLD * 0.5
