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
    PRUNE_PROTECTED_RECENT_MESSAGES,
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
    """旧工具大结果 → 未命中技能摘要 → 低相关记忆 → 旧会话的结构化压缩."""
    assert TRIMMING_ORDER == (
        SegmentName.OLD_TOOL_RESULTS,
        SegmentName.SKILL_SUMMARIES,
        SegmentName.MEMORY,
        SegmentName.RECENT_HISTORY,
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


def test_the_oldest_tool_results_are_the_first_thing_trimmed() -> None:
    """Step one of the fixed order, and the only one with content in M2A."""
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
    assert [record.segment for record in result.trimmed] == [SegmentName.OLD_TOOL_RESULTS]
    assert result.trimmed[0].references == ("c1", "c2")
    assert result.compacted is None


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


def test_trimming_stops_as_soon_as_the_round_fits() -> None:
    """Oldest first, and no further — the newest output is the last to go."""
    history = stored(
        says("start"),
        called("./one", "c1"),
        answered("a" * 30_000, "c1"),
        called("./two", "c2"),
        answered("b" * 400, "c2"),
    )
    result = plan(history, ContextWindow(9_000, reserved_output_tokens=1_000))

    assert result.fits is True
    assert result.trimmed[0].references == ("c1",)
    # The most recent result is still whole, because giving up the older one
    # was enough.
    assert result.messages[-1] == history[-1].message


def test_a_long_conversation_is_compacted_and_the_range_is_recorded() -> None:
    """Step four, and the record §7.4.2 requires it to leave."""
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

    Message count alone would stop at the smallest boundary that fits, and
    that is `through=2` here — right between the call and its answer, as
    `test_the_search_settles_early_when_there_is_room` in
    `test_stored_summary.py` shows for the plain-message version of this same
    shape. The fix must walk past the pair instead, to `through=3`.
    """
    history = stored(
        says("the task, stated at some length: " + "t" * 550),
        called("./step-0", "c0"),
        answered("ok", "c0"),
        *(says(f"round {index}: " + "w" * 200, role="assistant") for index in range(4)),
        says("what is left?"),
    )
    result = plan(history, ContextWindow(1_200, reserved_output_tokens=200))

    assert result.fits is True
    assert _no_orphaned_tool_results(result.messages)
    compaction = result.compacted
    assert compaction is not None
    # Advanced past the pair, not stopped short of it: the call's answer
    # (sequence 3) is inside the covered range too, not left standing alone.
    assert compaction.last_sequence >= 3
    assert compaction.message_ids == tuple(
        item.id for item in history[: compaction.covered]
    )


def test_when_every_boundary_would_split_a_pair_the_originals_are_kept() -> None:
    """No legal boundary exists in [2, compactable] at all: the call sits
    right after the start of the compactable range, its answer sits just
    outside it (protected by `PROTECTED_RECENT_MESSAGES`), and every
    candidate boundary in between would have to include the call without its
    answer.

    §7.4.2's failure ladder still applies: this round already fits `allowance`
    without any compaction, so nothing may be dropped and the Run may not be
    paused just because structural compaction found no legal place to cut —
    `threshold=0.0` forces the cascade to run anyway, to prove step four's
    empty search is what produces this result, not the round fitting from the
    start.
    """
    history = stored(
        says("start the task"),
        called("./step-0", "c0"),
        says("padding a", role="assistant"),
        says("padding b", role="assistant"),
        says("padding d", role="assistant"),
        answered("ok", "c0"),
        says("what is left?"),
    )
    result = plan(history, ROOMY, threshold=0.0)

    assert result.fits is True
    assert result.compacted is None
    assert result.messages == tuple(item.message for item in history)


def test_the_summary_says_what_it_replaced_and_where_to_find_it() -> None:
    """Structured, not written by a model: assertable, free, and repeatable."""
    rounds: list[CanonicalMessage] = []
    for index in range(6):
        rounds.append(called(f"./step-{index}", f"c{index}"))
        rounds.append(answered("q" * 3_000, f"c{index}"))
    history = stored(says("do it"), *rounds, says("status?"))
    window = ContextWindow(1_000, reserved_output_tokens=750)
    result = plan(history, window)

    assert result.compacted is not None
    summary = result.messages[0]
    assert summary.author == "platform"
    assert "compacted" in summary.text
    assert "shell.exec" in summary.text
    # Same input, same output. A summary a model wrote would not have this.
    assert plan(history, window).messages[0].text == summary.text


def test_a_compaction_never_reaches_the_request_it_is_making_room_for() -> None:
    """§7.4.2: 当前用户请求必须完整保留 — including from step four.

    A fresh Run states its request first and everything after it is the work,
    so oldest-first compaction walks straight at the one message that may not
    go. It stops instead, and this conversation overflows with its originals
    intact rather than fitting by summarizing the question away.
    """
    history = stored(
        says("the task: " + "t" * 600),
        *(says(f"round {index}: " + "w" * 900, role="assistant") for index in range(8)),
    )
    result = plan(history, ContextWindow(1_200, reserved_output_tokens=200))

    assert result.compacted is None
    assert result.fits is False
    assert result.messages == tuple(item.message for item in history)


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
    """压缩失败后保留原文. Nothing is deleted on the way to the pause."""
    history = stored(
        says("start"),
        says("m" * 40_000, role="assistant"),
        says("n" * 40_000, role="assistant"),
        says("the question"),
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
# 主动裁剪（§7.4.2「主动裁剪：不花钱的那几级不等压缩线」）
#
# 这一组守的是同一件事的五个面：不花钱的裁剪有自己的触发点、按条数保护尾部、
# 三遍确定性处理、以及两道缓存闸门。参照实现见 spec 里点名的
# `hermes-agent @ 3f83297` 的 `prune_tool_results_only`。
# ---------------------------------------------------------------------------


def _wide() -> ContextWindow:
    """一个大到压缩线永远够不着的窗口——这正是主动裁剪要解决的处境。

    1M 窗口、阈值 0.50 意味着要攒到 500K 才触发压缩；下面每条测试的历史都远小于
    那个数，所以任何在旧判据下发生的裁剪都只可能来自主动裁剪这条路径。
    """
    return ContextWindow(context_window=1_000_000, reserved_output_tokens=0)


def _huge(marker: str) -> str:
    """一段超过 `PRUNE_MIN_RESULT_CHARS` 的工具输出。

    `marker` 让每段内容互不相同，免得「去重」那一遍替「打存根」那一遍把测试蒙混过关。
    """
    return f"{marker}:" + ("x" * PRUNE_MIN_RESULT_CHARS)


def test_a_big_old_tool_result_is_pruned_long_before_the_compaction_line() -> None:
    """这条是整组的由来。

    旧实现里 `_trim_old_tool_results` 的目标是总额度，所以 1M 窗口下这段历史
    （几十 KB）离触发线差着三个数量级，一次裁剪都不会发生，而那段早就没用的输出
    每一轮都被逐字重发。
    """
    history = stored(
        *[
            message
            for index in range(12)
            for message in (
                says(f"问题 {index}"),
                called(f"cmd {index}", f"c{index}"),
                answered(_huge(f"out{index}"), f"c{index}"),
            )
        ]
    )
    plan_result = plan(history, _wide())

    assert plan_result.fits
    trimmed_segments = [record.segment for record in plan_result.trimmed]
    assert SegmentName.OLD_TOOL_RESULTS in trimmed_segments


def test_the_newest_messages_are_protected_by_count_not_by_tokens() -> None:
    """按 Token 保护尾部会在大窗口上护住整个会话，于是什么也裁不掉。

    Hermes 的 docstring 专门点了这个陷阱：`tail_token_budget` 是从压缩阈值推导的
    （1M 窗口上约 100K），用它做尾部保护等于把整段历史都算成「最近」。
    """
    history = stored(
        *[
            message
            for index in range(12)
            for message in (
                says(f"问题 {index}"),
                called(f"cmd {index}", f"c{index}"),
                answered(_huge(f"out{index}"), f"c{index}"),
            )
        ]
    )
    plan_result = plan(history, _wide())

    kept_whole = [
        block.output
        for message in plan_result.messages[-PRUNE_PROTECTED_RECENT_MESSAGES:]
        for block in message.blocks
        if isinstance(block, ToolResultBlock)
    ]
    # 尾部里的工具结果一个字都没动。
    assert all(len(output) > PRUNE_MIN_RESULT_CHARS for output in kept_whole)


def test_identical_tool_results_are_deduplicated_even_inside_the_protected_tail() -> None:
    """第一遍是无损的，所以它不受尾部保护限制。

    完全相同的输出重复出现时，最新的那份保留完整，更早的改成回指——模型能看到的
    信息一个字没少，而重复的字节不再每轮重发。
    """
    # 这段输出要够大：两道闸门是真的，夹具小于它们时正确的行为就是什么都不做。
    # 一段 8K 字符约 2K Token，回收量抵不过 `PRUNE_MIN_RECLAIM_TOKENS`，
    # 所以这里用远大于阈值的那种「一次 ls 刷了满屏」的量级。
    same = "identical:" + ("x" * (PRUNE_MIN_RESULT_CHARS * 6))
    history = stored(
        says("跑一遍"),
        called("ls", "c1"),
        answered(same, "c1"),
        says("再跑一遍"),
        called("ls", "c2"),
        answered(same, "c2"),
    )
    plan_result = plan(history, _wide())

    outputs = [
        block.output
        for message in plan_result.messages
        for block in message.blocks
        if isinstance(block, ToolResultBlock)
    ]
    assert len(outputs) == 2
    # 最新的那份完整保留，更早的那份不再重复承载同样的字节。
    assert outputs[-1] == same
    assert outputs[0] != same
    assert len(outputs[0]) < len(same)


def test_an_oversized_tool_call_argument_outside_the_tail_is_truncated() -> None:
    """第三遍：过大的工具调用参数也算重发的字节。

    只裁尾部之外的——模型正在依据的那次调用，参数必须原样。
    """
    giant = "giant-argument:" + ("x" * (PRUNE_MIN_RESULT_CHARS * 6))
    history = stored(
        called(giant, "c1"),
        answered("ok", "c1"),
        # 尾部保护按条数，所以那次调用必须真的被推出尾部才轮得到第三遍。
        *[
            message
            for index in range(PRUNE_PROTECTED_RECENT_MESSAGES + 2)
            for message in (says(f"后续 {index}"),)
        ],
    )
    plan_result = plan(history, _wide())

    first = plan_result.messages[0]
    argument = next(
        block.arguments["command"]
        for block in first.blocks
        if isinstance(block, ToolCallBlock)
    )
    # 比原长短，不是比阈值短：截断后的内容是「阈值长度 + 一句说明去哪儿取全文」，
    # 本来就会比阈值长一点。断言写成 `< PRUNE_MIN_RESULT_CHARS` 会把一个正确的
    # 实现判成失败。
    assert len(argument) < len(giant)
    assert "truncated by the platform" in argument


def test_a_prune_that_would_reclaim_almost_nothing_changes_nothing() -> None:
    """缓存闸门：改写历史会让 provider 的前缀缓存从最早被改写处失效。

    所以回收不够多就一个字不改——省下的那点 Token 抵不过一次缓存失效。
    断言的是「历史逐条相同」，不是「没有 trimmed 记录」：后者一个改了内容却忘了
    记录的实现也满足。
    """
    history = stored(
        *[
            message
            for index in range(12)
            for message in (
                says(f"问题 {index}"),
                called(f"cmd {index}", f"c{index}"),
                answered(f"短输出 {index}", f"c{index}"),
            )
        ]
    )
    plan_result = plan(history, _wide())

    assert plan_result.messages == tuple(item.message for item in history)
