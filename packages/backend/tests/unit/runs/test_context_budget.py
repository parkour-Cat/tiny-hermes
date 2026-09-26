"""What a round is allowed to send, decided before the call is made.

Product design §7.4.2 and M2A design §4.8–§4.9. The planner is a pure function
on purpose: every interesting case here — a window that only fits the
incompressible content, a tool result too large to carry, a conversation that
has to be compacted to fit — is one nobody wants to reproduce by driving a real
provider until it refuses.

Two properties run through the whole file rather than getting one test each,
because they are what the module exists for: the estimate is an upper bound and
is never called usage, and no branch loses a message.
"""

from typing import Any
from uuid import uuid4

from tiny_hermes.runs.domain.context_budget import (
    DEFAULT_SEGMENTS,
    PRUNE_MIN_RESULT_CHARS,
    RETAINED_TAIL_TOKENS,
    TRIMMING_ORDER,
    Accounting,
    ContextWindow,
    SegmentName,
    estimate_tokens,
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

#: Big enough that nothing is ever trimmed for it.
ROOMY = ContextWindow(context_window=1_000_000, reserved_output_tokens=1_000)


def stored(*messages: CanonicalMessage) -> tuple[StoredMessage, ...]:
    return tuple(
        StoredMessage(id=uuid4(), sequence=index, message=message)
        for index, message in enumerate(messages, start=1)
    )


def says(text: str, role: Any = "user") -> CanonicalMessage:
    return CanonicalMessage(role=role, blocks=(TextBlock(text=text),))


def called(command: str, call_id: str) -> CanonicalMessage:
    return CanonicalMessage(
        role="assistant",
        blocks=(
            ToolCallBlock(call_id=call_id, name="shell.exec", arguments={"command": command}),
        ),
    )


def answered(output: str, call_id: str) -> CanonicalMessage:
    return CanonicalMessage(
        role="tool",
        blocks=(ToolResultBlock(call_id=call_id, output=output, exit_code=0, failed=False),),
    )


def plan(history: tuple[StoredMessage, ...], window: ContextWindow, **extra: Any):
    return plan_context(
        window=window,
        safety_rules=RULES,
        personality=PERSONALITY,
        tool_schemas=(),
        history=history,
        **extra,
    )


def test_the_default_segments_are_the_table_in_the_product_design() -> None:
    """Seven rows, and the two nobody may trim are the two named as such."""
    assert set(DEFAULT_SEGMENTS) == {
        SegmentName.SAFETY_RULES,
        SegmentName.PERSONALITY,
        SegmentName.SKILL_SUMMARIES,
        SegmentName.MEMORY,
        SegmentName.TOOL_SCHEMAS,
        SegmentName.OLD_TOOL_RESULTS,
        SegmentName.RECENT_HISTORY,
    }
    assert DEFAULT_SEGMENTS[SegmentName.SAFETY_RULES].trimmable is False
    assert DEFAULT_SEGMENTS[SegmentName.PERSONALITY].trimmable is False
    # 最近历史 gets the remaining space rather than a number of its own.
    assert DEFAULT_SEGMENTS[SegmentName.RECENT_HISTORY].max_tokens is None


def test_the_trimming_order_is_the_fixed_one() -> None:
    """v2.10: 旧工具结果 → 旧会话压缩 → 未命中技能摘要 → 低相关记忆."""
    assert TRIMMING_ORDER == (
        SegmentName.OLD_TOOL_RESULTS,
        SegmentName.RECENT_HISTORY,
        SegmentName.SKILL_SUMMARIES,
        SegmentName.MEMORY,
    )


def test_the_estimate_is_an_upper_bound_on_both_kinds_of_text() -> None:
    """Not a guess at the real count: a low guess sends a request that is refused.

    A ratio tuned for English under-counts CJK roughly threefold, which is why
    the bound counts wide characters one for one.
    """
    assert estimate_tokens("hello world") >= len("hello world") / 4
    assert estimate_tokens("上下文预算与裁剪顺序") >= len("上下文预算与裁剪顺序")
    assert estimate_tokens("") == 0


def test_an_unverified_tokenizer_name_still_gets_an_answer() -> None:
    """No tokenizer ships verified, so every endpoint gets the bound.

    Declaring one that this platform has not verified must not silently become
    a number nothing stands behind — it falls back to the same bound.
    """
    assert estimate_tokens("hello", tokenizer="o200k_base") == estimate_tokens("hello")


def test_a_shared_window_reserves_the_output_and_a_separate_one_does_not() -> None:
    """§7.4.2: computed from what the endpoint declared, not from its name."""
    shared = ContextWindow(8_000, reserved_output_tokens=2_000)
    separate = ContextWindow(8_000, reserved_output_tokens=2_000, accounting=Accounting.SEPARATE)
    assert shared.input_allowance == 6_000
    assert separate.input_allowance == 8_000


def test_a_conversation_that_fits_is_sent_exactly_as_it_is() -> None:
    history = stored(says("do the thing"), says("working on it", role="assistant"))
    result = plan(history, ROOMY)
    assert result.fits is True
    assert result.changed is False
    assert result.messages == tuple(item.message for item in history)


def test_on_a_small_window_every_big_result_gives_way_rather_than_pause() -> None:
    """Both results are too big for this window. The older one, outside the
    retained tail, is cleared first; the newer one sits in the tail, and on a
    window this small the last rung stubs it too rather than pause."""
    history = stored(
        says("run the suite"),
        called("./one", "c1"),
        answered("x" * 20_000, "c1"),
        called("./two", "c2"),
        answered("y" * 20_000, "c2"),
        says("keep going"),
    )
    result = plan(history, ContextWindow(6_000, reserved_output_tokens=1_000))

    assert result.fits is True
    assert {record.segment for record in result.trimmed} == {SegmentName.OLD_TOOL_RESULTS}
    assert {
        reference for record in result.trimmed for reference in record.references
    } == {"c1", "c2"}
    assert _no_orphaned_tool_results(result.messages)


def test_a_trimmed_result_keeps_its_call_and_says_what_was_taken() -> None:
    """§7.4.2: 保留原始引用. A hole where a result was is worse than a stub.

    The tool message stays in place with the same ``call_id``, so the call that
    asked for it is still answered — a call and its result are never split —
    and the model is told the output existed rather than left to conclude the
    command never ran.
    """
    history = stored(
        says("run it"),
        called("./one", "c1"),
        answered("z" * 40_000, "c1"),
        says("and again"),
    )
    result = plan(history, ContextWindow(4_000, reserved_output_tokens=500))

    trimmed = result.messages[2]
    assert trimmed.role == "tool"
    block = trimmed.blocks[0]
    assert isinstance(block, ToolResultBlock)
    assert block.call_id == "c1"
    assert "40000" in block.output
    assert "c1" in block.output
    # The assistant turn that asked is untouched, so the pair is still a pair.
    assert result.messages[1] == history[1].message


def test_the_last_rung_stops_as_soon_as_the_round_fits() -> None:
    """Inside the tail, oldest first and no further — the newest output is the
    last to go."""
    history = stored(
        says("start"),
        called("./one", "c1"),
        answered("a" * 30_000, "c1"),
        called("./two", "c2"),
        answered("b" * 400, "c2"),
    )
    result = plan(history, ContextWindow(9_000, reserved_output_tokens=1_000))

    assert result.fits is True
    assert result.trimmed[-1].references == ("c1",)
    # The most recent result is still whole, because giving up the older one
    # was enough.
    assert result.messages[-1] == history[-1].message


def test_a_long_conversation_is_compacted_and_the_range_is_recorded() -> None:
    """The record §7.4.2 requires a compaction to leave."""
    history = stored(
        says("the task, stated at length: " + "t" * 600),
        *(says(f"round {index}: " + "w" * 600, role="assistant") for index in range(8)),
        says("what is left?"),
    )
    result = plan(history, ContextWindow(1_200, reserved_output_tokens=200))

    assert result.fits is True
    compaction = result.compacted
    assert compaction is not None
    assert compaction.first_sequence == 1
    assert compaction.covered >= 2
    assert compaction.message_ids == tuple(
        item.id for item in history[: compaction.covered]
    )
    assert compaction.last_sequence == history[compaction.covered - 1].sequence
    # The current request survives whole, and the summary stands where the
    # covered turns did rather than beside them.
    assert result.messages[-1] == history[-1].message
    assert len(result.messages) == len(history) - compaction.covered + 1


def _no_orphaned_tool_results(messages: tuple[CanonicalMessage, ...]) -> bool:
    """A `tool` message may only appear after the call it answers.

    Mirrors what an OpenAI-shaped provider actually checks: every
    `ToolResultBlock.call_id` in this list must have been introduced by a
    `ToolCallBlock` in an earlier message of the *same* list — the summary
    message a compaction inserts does not carry one, so a result surviving
    behind it with no call ahead is exactly the shape the provider rejected.
    """
    seen: set[str] = set()
    for message in messages:
        for block in message.blocks:
            if isinstance(block, ToolResultBlock) and block.call_id not in seen:
                return False
            if isinstance(block, ToolCallBlock):
                seen.add(block.call_id)
    return True


def test_a_compaction_boundary_never_splits_a_tool_call_from_its_result() -> None:
    """The shape that broke a real Feishu run: compaction picked at message
    count alone chose to cut between a `tool_calls` message and the `tool`
    message answering it, and the provider rejected the whole request —
    'Messages with role tool must be a response to a preceding message with
    tool_calls'. §7.4.2: 工具调用与工具结果不能拆开.

    Here the retained tail's token target lands on the result; the cut moves
    earlier, to the call.
    """
    history = stored(
        says("the task, stated at some length: " + "t" * 550),
        *(says(f"round {index}: " + "w" * 900, role="assistant") for index in range(6)),
        called("./step-0", "c0"),
        answered("r" * 3_000, "c0"),
        says("what is left?"),
    )
    result = plan(history, ContextWindow(2_400, reserved_output_tokens=200))

    assert result.fits is True
    assert _no_orphaned_tool_results(result.messages)
    assert result.compacted is not None


def test_the_summary_says_what_it_replaced_and_where_to_find_it() -> None:
    """Structured, not written by a model: assertable, free, and repeatable."""
    rounds: list[CanonicalMessage] = []
    for index in range(6):
        rounds.append(called(f"./step-{index}", f"c{index}"))
        # Distinct outputs: identical ones would be deduplicated below the
        # trigger by cleanup alone, and this test is about the summary.
        rounds.append(answered(f"{index}" + "q" * 3_000, f"c{index}"))
    history = stored(says("do it"), *rounds, says("status?"))
    window = ContextWindow(5_000, reserved_output_tokens=750)
    result = plan(history, window)

    assert result.compacted is not None
    summary = result.messages[0]
    assert summary.author == "platform"
    assert "compacted" in summary.text
    assert "shell.exec" in summary.text
    # Same input, same output. A summary a model wrote would not have this.
    assert plan(history, window).messages[0].text == summary.text


def test_incompressible_content_that_does_not_fit_does_not_get_truncated() -> None:
    """The one case §7.4.2 answers with a pause rather than a smaller request.

    The planner does not decide the Run's state — it reports that nothing it is
    allowed to do would make this fit, and hands back the originals untouched.
    """
    history = stored(says("x" * 50_000))
    result = plan(history, ContextWindow(1_000, reserved_output_tokens=200))

    assert result.fits is False
    assert result.messages == (history[0].message,)
    assert result.input_estimate > result.allowance


def test_a_conversation_that_cannot_be_compacted_small_enough_keeps_its_originals() -> None:
    """压缩失败后保留原文. Nothing is deleted on the way to the pause.

    The newest message is always kept whole, and here it is text no rung may
    cut — the one shape where compacting everything else is still not enough.
    """
    history = stored(
        says("the question"),
        says("m" * 40_000, role="assistant"),
        says("n" * 40_000, role="assistant"),
    )
    result = plan(history, ContextWindow(600, reserved_output_tokens=100))

    assert result.fits is False
    assert result.messages == tuple(item.message for item in history)


def test_the_planner_never_reports_a_number_as_usage() -> None:
    """The fields are named for what they are, so a caller cannot confuse them.

    §4.8: the planner decides what to send, never what to bill. `UsageQuality`
    still has no `estimated` member and nothing here produces one.
    """
    result = plan(stored(says("hello")), ROOMY)
    assert not hasattr(result, "tokens")
    assert not hasattr(result, "usage")
    assert result.input_estimate > 0


# ---------------------------------------------------------------------------
# 清理（§7.4.2 v2.10「① 清理」）：三遍确定性处理与缓存闸门。只在视图越过触发线时
# 发生，只动保留区之外——v2.9 的「主动裁剪」有自己的触发点、按条数保护尾部，
# 这两条 v2.10 取消了，对应的测试见 `test_compaction_v2.py`。
# ---------------------------------------------------------------------------

#: 触发线 85K（输入额度 100K × 0.85）；下面的夹具都在它之上。
_WINDOW = ContextWindow(context_window=101_000, reserved_output_tokens=1_000)


def _tail() -> tuple[CanonicalMessage, ...]:
    """一段刚好填满保留区的最近对话，让夹具里更早的东西都落在保留区之外。"""
    return (says("recent " + "r" * int(RETAINED_TAIL_TOKENS * 3 / 1.1)),)


def test_identical_tool_results_outside_the_tail_point_at_the_copy_inside_it() -> None:
    """第一遍是无损的：更早的那份改成回指，指向保留区里一字不差、完整发送的那份。
    这一次清理就够让这一轮回到触发线以下，所以不做摘要，两份都还在。"""
    same = "identical:" + ("x" * 29_000)
    filler = [says("f" * 30_000, role="assistant") for _ in range(4)]
    history = stored(
        says("跑一遍"),
        called("ls", "c1"),
        answered(same, "c1"),
        *filler,
        *_tail(),
        called("ls", "c2"),
        answered(same, "c2"),
    )
    plan_result = plan(history, _WINDOW)

    assert plan_result.compacted is None
    outputs = {
        block.call_id: block.output
        for message in plan_result.messages
        for block in message.blocks
        if isinstance(block, ToolResultBlock)
    }
    assert outputs["c2"] == same
    assert "c2" in outputs["c1"]
    assert len(outputs["c1"]) < len(same)


def test_an_oversized_tool_call_argument_outside_the_tail_is_truncated() -> None:
    """第三遍：过大的工具调用参数也算重发的字节。只裁保留区之外的——模型正在
    依据的那次调用，参数必须原样。"""
    giant = "giant-argument:" + ("x" * (PRUNE_MIN_RESULT_CHARS * 6))
    filler = [says("f" * 30_000, role="assistant") for _ in range(5)]
    history = stored(called(giant, "c1"), answered("ok", "c1"), *filler, *_tail())
    plan_result = plan(history, _WINDOW)

    first = next(
        block
        for message in plan_result.messages
        for block in message.blocks
        if isinstance(block, ToolCallBlock) and block.call_id == "c1"
    )
    argument = first.arguments["command"]
    # 比原长短，不是比阈值短：截断后的内容是「阈值长度 + 一句说明去哪儿取全文」。
    assert len(argument) < len(giant)
    assert "truncated by the platform" in argument


def test_a_cleanup_that_would_reclaim_almost_nothing_is_not_recorded() -> None:
    """缓存闸门：回收不够多就不采纳这次清理——省下的那点 Token 抵不过一次缓存失效。
    这一轮越过了触发线，所以接下来轮到压缩；清理本身不留下任何记录。"""
    filler = [says("f" * 30_000, role="assistant") for _ in range(9)]
    history = stored(
        called("cmd", "c1"),
        answered("短输出 " + "s" * (PRUNE_MIN_RESULT_CHARS + 10), "c1"),
        *filler,
        *_tail(),
    )
    plan_result = plan(history, _WINDOW)

    assert SegmentName.OLD_TOOL_RESULTS not in {
        record.segment for record in plan_result.trimmed
    }
