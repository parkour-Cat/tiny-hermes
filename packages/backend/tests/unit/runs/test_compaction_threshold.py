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
