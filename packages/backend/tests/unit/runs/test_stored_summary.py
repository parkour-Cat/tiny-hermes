"""压缩用传进来的那份摘要，而不是每轮现编一份。

现编会同时坏两件事：同一个 Run 重放得到不同的上下文，以及每轮多付一次模型
调用。所以摘要是输入，不是这一层的产物。
"""

from typing import Any
from uuid import uuid4

import pytest
from tiny_hermes.runs.domain.context_budget import (
    ContextWindow,
    CoveredSummary,
    plan_context,
)
from tiny_hermes.runs.domain.models import CanonicalMessage, StoredMessage, TextBlock

RULES = "Stay inside the platform."
PERSONALITY = "You are a careful assistant."

#: Small enough that the long fixture below has to be compacted to fit —
#: `test_context_budget.py` uses the same shape of window for the same reason.
WINDOW = ContextWindow(1_200, reserved_output_tokens=200)


def _stored(*messages: CanonicalMessage) -> tuple[StoredMessage, ...]:
    return tuple(
        StoredMessage(id=uuid4(), sequence=index, message=message)
        for index, message in enumerate(messages, start=1)
    )


def _says(text: str, role: Any = "user") -> CanonicalMessage:
    return CanonicalMessage(role=role, blocks=(TextBlock(text=text),))


@pytest.fixture
def long_history() -> tuple[StoredMessage, ...]:
    """Long enough that `WINDOW` forces step four: structural compaction.

    Same shape as `test_a_long_conversation_is_compacted_and_the_range_is_recorded`
    in `test_context_budget.py` — a long stated task, several padded rounds, and
    a final request that must survive whole.
    """
    return _stored(
        _says("the task, stated at length: " + "t" * 600),
        *(_says(f"round {index}: " + "w" * 600, role="assistant") for index in range(8)),
        _says("what is left?"),
    )


@pytest.fixture
def short_history() -> tuple[StoredMessage, ...]:
    """Small enough that `WINDOW` never has to compact anything."""
    return _stored(_says("hello"), _says("hi there", role="assistant"))


def _plan_with(
    history: tuple[StoredMessage, ...], *, stored_summary: CoveredSummary | None
):
    return plan_context(
        window=WINDOW,
        safety_rules=RULES,
        personality=PERSONALITY,
        tool_schemas=(),
        history=history,
        stored_summary=stored_summary,
    )


def _first_text(messages: tuple[CanonicalMessage, ...]) -> str:
    block = messages[0].blocks[0]
    assert isinstance(block, TextBlock)
    return block.text


def _all_text(messages: tuple[CanonicalMessage, ...]) -> list[str]:
    return [
        block.text
        for message in messages
        for block in message.blocks
        if isinstance(block, TextBlock)
    ]


def test_a_stored_summary_is_what_the_model_sees(
    long_history: tuple[StoredMessage, ...],
) -> None:
    plan = _plan_with(
        long_history,
        stored_summary=CoveredSummary(text="用户在排查一条图片管道的故障。", last_sequence=7),
    )

    assert plan.compacted is not None
    text = _first_text(plan.messages)
    assert "用户在排查一条图片管道的故障。" in text
    assert plan.compacted.source == "model"


def test_without_one_it_falls_back_to_the_structural_summary(
    long_history: tuple[StoredMessage, ...],
) -> None:
    plan = _plan_with(long_history, stored_summary=None)

    assert plan.compacted is not None
    text = _first_text(plan.messages)
    assert "compacted by the platform" in text
    assert plan.compacted.source == "structural"


def test_the_stored_summary_is_not_used_when_nothing_is_compacted(
    short_history: tuple[StoredMessage, ...],
) -> None:
    plan = _plan_with(
        short_history, stored_summary=CoveredSummary(text="不该出现", last_sequence=1)
    )

    assert plan.compacted is None
    assert "不该出现" not in "".join(_all_text(plan.messages))


@pytest.fixture
def room_to_spare() -> tuple[StoredMessage, ...]:
    """Long enough to compact, short enough that compacting the first two
    turns is already enough — so the boundary search stops well before the end
    of any range a stored summary is likely to cover."""
    return _stored(
        _says("the task, stated at some length: " + "t" * 550),
        *(_says(f"round {index}: " + "w" * 200, role="assistant") for index in range(5)),
        _says("what is left?"),
    )


def test_the_search_settles_early_when_there_is_room(
    room_to_spare: tuple[StoredMessage, ...],
) -> None:
    """Pinned so the test below has a number to be about: without a stored
    summary this shape compacts two turns and stops, because two is enough."""
    plan = _plan_with(room_to_spare, stored_summary=None)

    assert plan.compacted is not None
    assert plan.compacted.last_sequence == 2


def test_a_stored_summary_compacts_exactly_the_range_it_explains(
    room_to_spare: tuple[StoredMessage, ...],
) -> None:
    """一份解释 1–5 的摘要，不能只顶替 1–2。

    顶替少了不丢东西，坏的是另外两件：模型同时读到一份说「1–5 发生了这些」的
    摘要和 3–5 的原文；`CONTEXT_COMPACTED` 说 `covered=2`、`freed_estimate`
    也按两条算——运维照着这条记录去查模型读到了什么，读到的是一份少说了三条的
    账。

    所以边界不是搜出来的：摘要写下来时就已经说明了它解释到哪，`plan_context`
    钉在那里，装不下就整份不用（`_honestly_widens` 那条回退路）。
    """
    covered = CoveredSummary(text="1 到 5 轮里用户确认了参数并让我继续。", last_sequence=5)

    plan = _plan_with(room_to_spare, stored_summary=covered)

    assert plan.fits
    assert plan.compacted is not None
    assert plan.compacted.last_sequence == 5
    assert plan.compacted.covered == 5
    assert "1 到 5 轮里用户确认了参数并让我继续。" in _first_text(plan.messages)
    # 3–5 顶替掉了，不该再以原文出现一遍。
    assert "round 2: " not in "".join(_all_text(plan.messages))
