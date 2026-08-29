"""压缩用传进来的那份摘要，而不是每轮现编一份。

现编会同时坏两件事：同一个 Run 重放得到不同的上下文，以及每轮多付一次模型
调用。所以摘要是输入，不是这一层的产物。
"""

from typing import Any
from uuid import uuid4

import pytest
from tiny_hermes.runs.domain.context_budget import ContextWindow, plan_context
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
    history: tuple[StoredMessage, ...], *, stored_summary: str | None
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
    plan = _plan_with(long_history, stored_summary="用户在排查一条图片管道的故障。")

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
    plan = _plan_with(short_history, stored_summary="不该出现")

    assert plan.compacted is None
    assert "不该出现" not in "".join(_all_text(plan.messages))
