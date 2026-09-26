"""压缩用传进来的那份摘要，而不是每轮现编一份；而且它是存档点。

现编会同时坏两件事：同一个 Run 重放得到不同的上下文，以及每轮多付一次模型
调用。所以摘要是输入，不是这一层的产物。

v2.10 起它还是存档点：此后每轮发「摘要 + 补回 + 它之后的原文」，它覆盖的原文
不再发送。
"""

from typing import Any
from uuid import uuid4

import pytest
from tiny_hermes.runs.domain.context_budget import (
    ContextWindow,
    CoveredSummary,
    plan_context,
)
from tiny_hermes.runs.domain.models import (
    CanonicalMessage,
    StoredMessage,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)

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


def _called(command: str, call_id: str) -> CanonicalMessage:
    return CanonicalMessage(
        role="assistant",
        blocks=(
            ToolCallBlock(call_id=call_id, name="shell.exec", arguments={"command": command}),
        ),
    )


def _answered(output: str, call_id: str) -> CanonicalMessage:
    return CanonicalMessage(
        role="tool",
        blocks=(ToolResultBlock(call_id=call_id, output=output, exit_code=0, failed=False),),
    )


@pytest.fixture
def long_history() -> tuple[StoredMessage, ...]:
    """Long enough that `WINDOW` forces a structural compaction.

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

    assert plan.checkpoint == 7
    text = _first_text(plan.messages)
    assert "用户在排查一条图片管道的故障。" in text


def test_without_one_it_falls_back_to_the_structural_summary(
    long_history: tuple[StoredMessage, ...],
) -> None:
    plan = _plan_with(long_history, stored_summary=None)

    assert plan.compacted is not None
    text = _first_text(plan.messages)
    assert "compacted by the platform" in text
    assert plan.compacted.source == "structural"


def test_the_originals_a_stored_summary_covers_are_not_sent_again() -> None:
    """A checkpoint, even on a history that would not need compacting: the
    Session was compacted once, and the turns the summary stands for stay
    replaced. (The user's own words come back inside the summary message's
    put-back, not as the message they were.)"""
    history = _stored(_says("hello"), _says("hi there", role="assistant"), _says("and now?"))
    plan = _plan_with(
        history, stored_summary=CoveredSummary(text="打过招呼。", last_sequence=1)
    )

    assert plan.checkpoint == 1
    sent = _all_text(plan.messages)
    assert "hello" not in sent
    assert "hi there" in sent
    assert "打过招呼。" in _first_text(plan.messages)


@pytest.fixture
def room_to_spare() -> tuple[StoredMessage, ...]:
    """Short enough to fit without compacting."""
    return _stored(
        _says("the task, stated at some length: " + "t" * 550),
        *(_says(f"round {index}: " + "w" * 200, role="assistant") for index in range(5)),
        _says("what is left?"),
    )


def test_a_stored_summary_stands_for_exactly_the_range_it_explains(
    room_to_spare: tuple[StoredMessage, ...],
) -> None:
    """一份解释 1–5 的摘要，顶替的就是 1–5：模型不会同时读到「1–5 发生了这些」和
    其中几条的原文，也不会少读 6 之后的原文。"""
    covered = CoveredSummary(text="1 到 5 轮里用户确认了参数并让我继续。", last_sequence=5)

    plan = _plan_with(room_to_spare, stored_summary=covered)

    assert plan.fits
    assert plan.checkpoint == 5
    sent = "".join(_all_text(plan.messages))
    assert "1 到 5 轮里用户确认了参数并让我继续。" in sent
    assert "round 3: " not in sent  # sequence 5
    assert "round 4: " in sent  # sequence 6


def test_a_summary_ending_between_a_call_and_its_result_is_not_a_checkpoint() -> None:
    """Built on it, the view would send the result (sequence 3) with its call
    (sequence 2) summarized away — the shape a provider rejects outright. The
    Worker never saves such a summary (its cut moves earlier instead), but a
    stored row is not proof of who wrote it."""
    history = _stored(
        _says("the task, stated at some length: " + "t" * 550),
        _called("./step-0", "c0"),
        _answered("ok", "c0"),
        *(_says(f"round {index}: " + "w" * 200, role="assistant") for index in range(4)),
        _says("what is left?"),
    )
    covered = CoveredSummary(text="用户让我跑一个命令。", last_sequence=2)

    plan = _plan_with(history, stored_summary=covered)

    assert plan.checkpoint is None
    assert "用户让我跑一个命令。" not in "".join(_all_text(plan.messages))
