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
    MESSAGE_OVERHEAD_TOKENS,
    MIN_COMPACTION_GAIN_TOKENS,
    PRUNE_HEAD_CHARS,
    PRUNE_MIN_RESULT_CHARS,
    RETAINED_TAIL_TOKENS,
    SKILL_LOAD_TOOL,
    SKILL_REINJECT_MAX_TOKENS,
    TOOL_RESULT_HEAD_CHARS,
    TOOL_RESULT_MAX_CHARS,
    TOOL_RESULT_TAIL_CHARS,
    ContextWindow,
    CoveredSummary,
    SkillSummary,
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
from tiny_hermes.tools.domain.registry import (
    IMPLEMENTED_TOOLS,
    SKILL_LOAD_SCHEMA,
    UNTRIMMED_TOOLS,
)

RULES = "Stay inside the platform."
PERSONALITY = "You are a careful assistant."

#: 100,000 input tokens: small enough to fill with a readable fixture, large
#: enough that the retained tail is its default size rather than a fraction.
WINDOW = ContextWindow(context_window=101_000, reserved_output_tokens=1_000)
THRESHOLD = 0.85
TRIGGER = WINDOW.input_allowance * THRESHOLD

SKILL_TOOLS = frozenset({"skill.load"})


def _stored(*messages: CanonicalMessage, start: int = 1) -> tuple[StoredMessage, ...]:
    return tuple(
        StoredMessage(id=uuid4(), sequence=index, message=message)
        for index, message in enumerate(messages, start=start)
    )


def _says(text: str, role: Any = "user") -> CanonicalMessage:
    return CanonicalMessage(role=role, blocks=(TextBlock(text=text),))


def _called(call_id: str, name: str = "shell.exec", **arguments: Any) -> CanonicalMessage:
    return CanonicalMessage(
        role="assistant",
        blocks=(ToolCallBlock(call_id=call_id, name=name, arguments=arguments or {"c": "ls"}),),
    )


def _answered(call_id: str, output: str) -> CanonicalMessage:
    return CanonicalMessage(
        role="tool",
        blocks=(ToolResultBlock(call_id=call_id, output=output, exit_code=0, failed=False),),
    )


def _words(tokens: int, marker: str = "") -> str:
    """ASCII text whose estimate is close to ``tokens``."""
    unit = "lorem ipsum dolor sit amet "
    text = marker + " " + unit * max(int(tokens * 3 / 1.1) // len(unit), 1)
    return text


def _plan(
    history: tuple[StoredMessage, ...], *, threshold: float = THRESHOLD, **extra: Any
):
    extra.setdefault("untrimmed_tools", SKILL_TOOLS)
    return plan_context(
        window=WINDOW,
        safety_rules=RULES,
        personality=PERSONALITY,
        tool_schemas=(),
        history=history,
        threshold=threshold,
        **extra,
    )


def _raw(history: tuple[StoredMessage, ...]) -> int:
    """What the round would cost untouched: planned against a window it
    cannot come near, so nothing is cleared or compacted."""
    return plan_context(
        window=ContextWindow(context_window=100_000_000, reserved_output_tokens=1_000),
        safety_rules=RULES,
        personality=PERSONALITY,
        tool_schemas=(),
        history=history,
    ).input_estimate


def _conversation(turns: int, *, start: int = 1) -> tuple[StoredMessage, ...]:
    """Alternating user/assistant turns of roughly 1,500 estimated tokens each."""
    messages = [
        _says(
            _words(1_500, f"turn {index}"),
            role="user" if index % 2 == 0 else "assistant",
        )
        for index in range(turns)
    ]
    messages.append(_says("what should we do next?"))
    return _stored(*messages, start=start)


def _just_over_the_trigger() -> tuple[StoredMessage, ...]:
    """The shortest conversation whose uncompacted estimate crosses the trigger."""
    for turns in range(2, 400):
        history = _conversation(turns)
        untouched = _raw(history)
        if untouched > TRIGGER:
            assert untouched < TRIGGER * 1.05
            return history
    raise AssertionError("fixture never crossed the trigger")


def _orphans(messages: tuple[CanonicalMessage, ...]) -> list[str]:
    """Tool results sent without the call that asked for them."""
    called: set[str] = set()
    orphaned: list[str] = []
    for message in messages:
        for block in message.blocks:
            if isinstance(block, ToolCallBlock):
                called.add(block.call_id)
            elif isinstance(block, ToolResultBlock) and block.call_id not in called:
                orphaned.append(block.call_id)
    return orphaned


def _verbatim(plan_messages: tuple[CanonicalMessage, ...], history: tuple[StoredMessage, ...]):
    """Which history rows went out as the very same message object."""
    sent = {id(message) for message in plan_messages}
    return [item for item in history if id(item.message) in sent]


# --- the trigger --------------------------------------------------------


def test_below_the_trigger_nothing_is_rewritten_at_all() -> None:
    """Not even an old tool result that the v2.9 proactive prune would have
    stubbed: a round below the trigger goes out byte for byte, so the
    provider's prefix cache keeps hitting."""
    big = "x" * (PRUNE_MIN_RESULT_CHARS * 3)
    history = _stored(
        _says("look at the log"),
        _called("c1"),
        _answered("c1", big),
        *[_says(_words(800, f"filler {index}"), role="assistant") for index in range(30)],
        _says("and now?"),
    )
    assert _raw(history) < TRIGGER

    result = _plan(history)

    assert result.messages == tuple(item.message for item in history)
    assert result.trimmed == ()
    assert result.compacted is None


def test_one_compaction_brings_the_round_well_below_the_trigger() -> None:
    history = _just_over_the_trigger()

    result = _plan(history)

    assert result.compacted is not None
    # Not merely "fits": below the trigger with room to grow, or the very next
    # round is over it again and compacts again.
    assert result.input_estimate < TRIGGER * 0.5


def test_an_absolute_cap_triggers_before_the_ratio_does() -> None:
    history = _conversation(20)
    assert _raw(history) < TRIGGER

    result = _plan(history, trigger_cap=20_000)

    assert result.compacted is not None


# --- the retained tail --------------------------------------------------


def test_the_newest_twenty_thousand_tokens_go_out_verbatim() -> None:
    history = _just_over_the_trigger()

    result = _plan(history)

    # messages[0] is the summary; the current request is in the tail, so
    # nothing is put back as a message of its own and the tail is the rest.
    tail = _verbatim(result.messages[1:], history)
    assert tail[-1] is history[-1]
    assert [item.sequence for item in tail] == list(
        range(tail[0].sequence, history[-1].sequence + 1)
    )
    assert len(tail) == len(result.messages) - 1
    sizes = [estimate_tokens(item.message.blocks[0].text) + MESSAGE_OVERHEAD_TOKENS
        for item in tail]  # type: ignore[union-attr]
    assert sum(sizes) >= RETAINED_TAIL_TOKENS
    # Whole messages, and the cut is at the first one that reaches the
    # target — not one further.
    assert sum(sizes[1:]) < RETAINED_TAIL_TOKENS


def test_the_tail_moves_earlier_rather_than_split_a_call_from_its_result() -> None:
    """The 20K point falls between a call and its (large) result: the call
    joins the tail instead of the result being sent without it."""
    history = _stored(
        *[
            _says(_words(1_500, f"old {index}"), role="user" if index % 2 == 0 else "assistant")
            for index in range(50)
        ],
        _called("c1"),
        _answered("c1", _words(19_000, "result")),
        _says(_words(1_500, "after")),
    )

    result = _plan(history)

    assert result.compacted is not None
    assert _orphans(result.messages) == []
    assert any(
        isinstance(block, ToolCallBlock) and block.call_id == "c1"
        for message in result.messages
        for block in message.blocks
    )


def test_a_long_run_of_tool_calls_after_one_request_can_be_compacted() -> None:
    """v2.10: everything after the last user message used to be
    uncompactable, so an agent loop that grew past the window could only
    pause. The request itself still goes out whole."""
    request = _says("migrate every service and report back")
    rounds: list[CanonicalMessage] = []
    for index in range(40):
        rounds.append(_called(f"c{index}"))
        rounds.append(_answered(f"c{index}", _words(2_200, f"output {index}")))
    history = _stored(request, *rounds)
    assert _raw(history) > TRIGGER

    result = _plan(history)

    assert result.compacted is not None
    assert result.input_estimate < TRIGGER * 0.5
    assert request in result.messages
    assert _orphans(result.messages) == []


# --- the stored summary is a checkpoint ---------------------------------


def _checkpointed(turns_before: int, turns_after: int):
    before = _conversation(turns_before)
    after = _conversation(turns_after, start=before[-1].sequence + 1)
    history = before + after
    summary = CoveredSummary("## 目标\nkeep the service up", before[-1].sequence)
    return history, before, after, summary


def test_after_a_compaction_the_next_round_reads_the_checkpoint() -> None:
    """The full history is still far over the trigger; the view — summary
    plus what came after it — is not, so nothing is compacted again."""
    history, before, after, summary = _checkpointed(60, 6)
    assert _raw(history) > TRIGGER

    result = _plan(history, stored_summary=summary)

    assert result.compacted is None
    assert result.checkpoint == before[-1].sequence
    assert "keep the service up" in result.messages[0].blocks[0].text  # type: ignore[union-attr]
    assert result.messages[0].author == "platform"
    for item in after:
        assert item.message in result.messages
    # The newest covered user message is put back whole; nothing else from
    # the covered range goes out as itself.
    covered_originals = [item for item in _verbatim(result.messages, history) if item in before]
    assert len(covered_originals) <= 1


def test_the_checkpoint_view_is_identical_from_round_to_round() -> None:
    """The prefix the provider cached must still be the prefix next round."""
    history, _, _, summary = _checkpointed(60, 6)
    first = _plan(history, stored_summary=summary)
    later = history + _stored(_says("one more thing"), start=history[-1].sequence + 1)

    second = _plan(later, stored_summary=summary)

    assert second.messages[: len(first.messages)] == first.messages


def test_a_summary_for_turns_not_in_this_history_is_not_used() -> None:
    """An ephemeral Session hands a Run only its own turns; a summary another
    Run left behind names a sequence this history does not have."""
    history = _conversation(10, start=500)
    stray = CoveredSummary("from some other run", last_sequence=42)

    result = _plan(history, stored_summary=stray)

    assert result.checkpoint is None
    assert all(
        "from some other run" not in getattr(block, "text", "")
        for message in result.messages
        for block in message.blocks
    )


def test_a_checkpoint_that_grows_past_the_trigger_compacts_only_what_is_new() -> None:
    history, before, _, summary = _checkpointed(20, 70)

    result = _plan(history, stored_summary=summary)

    assert result.compacted is not None
    covered = {item.id: item for item in history}
    assert all(
        covered[value].sequence > before[-1].sequence
        for value in result.compacted.message_ids
    )
    # The record states the whole range the resulting summary stands for.
    assert result.compacted.first_sequence == history[0].sequence
    # The structural fallback continues the stored text rather than replacing it.
    assert "keep the service up" in result.compacted.summary


# --- when not to compact ------------------------------------------------


def test_a_compaction_that_would_free_almost_nothing_is_not_made() -> None:
    """The fixed segments are what is large, not the history: all a
    compaction could take is a few small turns outside the tail."""
    history = _stored(
        _says("hello"),
        _says("hi", role="assistant"),
        _says(_words(RETAINED_TAIL_TOKENS + 1_000, "recent")),
    )
    personality = _words(70_000, "persona")

    result = plan_context(
        window=WINDOW,
        safety_rules=RULES,
        personality=personality,
        tool_schemas=(),
        history=history,
        threshold=THRESHOLD,
    )

    assert result.input_estimate > TRIGGER
    assert result.compacted is None
    assert result.fits is True


def test_compact_on_request_ignores_the_trigger() -> None:
    history = _conversation(30)
    assert _raw(history) < TRIGGER

    result = _plan(history, forced=True)

    assert result.compacted is not None


def test_compact_on_request_with_nothing_outside_the_tail_says_so() -> None:
    history = _conversation(4)

    result = _plan(history, forced=True)

    assert result.compacted is None
    assert result.compaction_skipped == "nothing_outside_tail"


def test_a_minimum_gain_is_the_documented_constant() -> None:
    assert MIN_COMPACTION_GAIN_TOKENS == 4_096


# --- cleanup before summarizing -----------------------------------------


def _tool_heavy(results: int, size: int, *, name: str = "shell.exec") -> tuple[StoredMessage, ...]:
    messages: list[CanonicalMessage] = [_says("dig through the logs")]
    for index in range(results):
        messages.append(_called(f"c{index}", name=name))
        messages.append(_answered(f"c{index}", f"line {index}\n" + "y" * size))
    messages.append(_says(_words(RETAINED_TAIL_TOKENS + 500, "tail")))
    return _stored(*messages)


def test_over_the_trigger_old_tool_output_is_cleared_first() -> None:
    """Enough to get back under the trigger, so no summary is needed."""
    history = _tool_heavy(12, 25_000)
    assert _raw(history) > TRIGGER

    result = _plan(history)

    assert result.compacted is None
    assert result.trimmed
    assert result.input_estimate < TRIGGER


def test_a_cleared_result_keeps_its_head() -> None:
    history = _tool_heavy(12, 25_000)

    result = _plan(history)

    first = next(
        block
        for message in result.messages
        for block in message.blocks
        if isinstance(block, ToolResultBlock) and block.call_id == "c0"
    )
    assert first.output.startswith("line 0\n" + "y" * (PRUNE_HEAD_CHARS - len("line 0\n")))
    assert "c0" in first.output
    assert len(first.output) < 25_000


def test_skill_text_is_never_cleared() -> None:
    """Cleanup clears the shell output around it and leaves the skill whole."""
    messages: list[CanonicalMessage] = [_says("dig through the logs")]
    messages += [_called("s", name="skill.load"), _answered("s", "S" * 25_000)]
    for index in range(10):
        messages.append(_called(f"c{index}"))
        messages.append(_answered(f"c{index}", "y" * 25_000))
    messages.append(_says(_words(RETAINED_TAIL_TOKENS + 500, "tail")))
    history = _stored(*messages)

    result = _plan(history)

    assert result.compacted is None
    assert result.trimmed
    skill = next(
        block
        for message in result.messages
        for block in message.blocks
        if isinstance(block, ToolResultBlock) and block.call_id == "s"
    )
    assert skill.output == "S" * 25_000


def test_the_tail_is_never_cleared() -> None:
    messages: list[CanonicalMessage] = [_says(_words(1_500, f"old {i}")) for i in range(60)]
    messages += [_called("fresh"), _answered("fresh", "z" * (PRUNE_MIN_RESULT_CHARS * 2))]
    messages.append(_says("what did it say?"))
    history = _stored(*messages)

    result = _plan(history)

    fresh = next(
        block
        for message in result.messages
        for block in message.blocks
        if isinstance(block, ToolResultBlock) and block.call_id == "fresh"
    )
    assert fresh.output == "z" * (PRUNE_MIN_RESULT_CHARS * 2)


# --- the entry cap ------------------------------------------------------


def test_an_oversized_tool_result_is_capped_from_its_first_send() -> None:
    output = "H" * TOOL_RESULT_HEAD_CHARS + "M" * 40_000 + "T" * TOOL_RESULT_TAIL_CHARS
    history = _stored(_says("run it"), _called("big"), _answered("big", output))

    result = _plan(history)

    capped = result.messages[2].blocks[0]
    assert isinstance(capped, ToolResultBlock)
    assert capped.output.startswith("H" * TOOL_RESULT_HEAD_CHARS)
    assert capped.output.endswith("T" * TOOL_RESULT_TAIL_CHARS)
    assert "M" * 100 not in capped.output
    assert str(len(output)) in capped.output
    # Below the trigger, and still no trim event: the model never saw the
    # whole output, so nothing it saw was rewritten.
    assert result.trimmed == ()
    assert len(output) > TOOL_RESULT_MAX_CHARS


def test_skill_text_is_not_capped() -> None:
    output = "S" * (TOOL_RESULT_MAX_CHARS + 10_000)
    history = _stored(
        _says("use the skill"), _called("s", name="skill.load"), _answered("s", output)
    )

    result = _plan(history)

    assert result.messages[2].blocks[0].output == output  # type: ignore[union-attr]


# --- what is put back ---------------------------------------------------


def _compacted_with(*covered: CanonicalMessage) -> tuple[tuple[StoredMessage, ...], Any]:
    history = _stored(
        *covered,
        *[
            _says(_words(1_500, f"filler {index}"), role="assistant")
            for index in range(60)
        ],
        _says("so?"),
    )
    return history, _plan(history)


def test_what_the_user_said_is_put_back_word_for_word() -> None:
    history, result = _compacted_with(
        _says("never touch the billing tables"),
        _says("ok", role="assistant"),
        _says("and answer in English"),
    )

    assert result.compacted is not None
    head = result.messages[0].blocks[0].text  # type: ignore[union-attr]
    assert "never touch the billing tables" in head
    assert "and answer in English" in head
    # Not the current request ("so?" is, and it is in the tail), so it is not
    # also sent as a message of its own.
    assert history[2].message not in result.messages


def test_the_put_back_shrinks_with_a_small_window() -> None:
    """A pasted document as an old user message must not be what makes a
    small window overflow after compaction — it is in the summary, and the
    put-back is capped at an eighth of the allowance."""
    window = ContextWindow(context_window=13_568, reserved_output_tokens=4_096)
    history = _stored(
        _says(_words(5_500, "a pasted document")),
        _says(_words(5_500, "reply"), role="assistant"),
        _says(_words(5_500, "another pasted document")),
        _says(_words(5_500, "reply"), role="assistant"),
        _says("and what is left?"),
    )

    result = plan_context(
        window=window,
        safety_rules=RULES,
        personality=PERSONALITY,
        tool_schemas=(),
        history=history,
        threshold=THRESHOLD,
    )

    assert result.compacted is not None
    assert result.fits is True
    head = result.messages[0].blocks[0].text  # type: ignore[union-attr]
    assert "pasted document" not in head


def test_a_loaded_skill_is_put_back_and_a_large_one_is_named() -> None:
    small = "step 1: do the thing"
    large = "B" * int(SKILL_REINJECT_MAX_TOKENS * 3 / 1.1 * 1.5)
    _, result = _compacted_with(
        _says("use both skills"),
        _called("s1", name="skill.load", skill="deploy", path="SKILL.md"),
        _answered("s1", small),
        _called("s2", name="skill.load", skill="handbook", path="SKILL.md"),
        _answered("s2", large),
    )

    assert result.compacted is not None
    head = result.messages[0].blocks[0].text  # type: ignore[union-attr]
    assert small in head
    assert "handbook" in head
    assert large not in head


def test_skill_summaries_are_not_dropped_when_a_compaction_is_enough() -> None:
    """v2.10 moves 未命中技能摘要 after compaction: dropping them buys less
    than one compaction and costs a capability every round."""
    history = _just_over_the_trigger()
    summaries = [SkillSummary(name=f"skill-{i}", text=f"skill {i} does a thing") for i in range(5)]

    result = _plan(history, skill_summaries=summaries)

    assert result.compacted is not None
    assert len(result.skill_summaries) == 5


def test_the_planner_and_the_registry_name_the_same_skill_tool() -> None:
    """The put-back recognizes skill text by tool name; the registry is where
    the name is defined, and where the exemption list is declared."""
    assert SKILL_LOAD_TOOL in IMPLEMENTED_TOOLS
    assert SKILL_LOAD_SCHEMA["function"]["name"] == SKILL_LOAD_TOOL
    assert SKILL_LOAD_TOOL in UNTRIMMED_TOOLS


# --- the last rungs -----------------------------------------------------


def test_a_small_window_stubs_a_big_result_inside_the_tail_rather_than_pause() -> None:
    """The retained tail is never touched while anything else can give — but
    on a small window one tool result can be larger than the whole room, and
    pausing is worse than sending its head."""
    window = ContextWindow(6_000, reserved_output_tokens=1_000)
    history = _stored(
        _says("run the suite"),
        _called("c1"),
        _answered("c1", "x" * 20_000),
        _called("c2"),
        _answered("c2", "y" * 20_000),
        _says("keep going"),
    )

    result = plan_context(
        window=window,
        safety_rules=RULES,
        personality=PERSONALITY,
        tool_schemas=(),
        history=history,
        threshold=THRESHOLD,
    )

    assert result.fits is True
    assert _orphans(result.messages) == []
    newest = next(
        block
        for message in result.messages
        for block in message.blocks
        if isinstance(block, ToolResultBlock) and block.call_id == "c2"
    )
    assert newest.output.startswith("y" * 100)
    assert "20000" in newest.output


def test_when_nothing_makes_it_fit_the_originals_come_back_uncompacted() -> None:
    """§7.4.2 保留原文: and no compaction record, so the Worker does not pay a
    summarizer for a round that is going to pause anyway — a model summary is
    never shorter than the structural one that already did not fit."""
    window = ContextWindow(600, reserved_output_tokens=100)
    # The newest message is kept whole whatever its size, and it is text —
    # nothing any rung may cut.
    history = _stored(
        _says("the question"),
        _says("m" * 40_000, role="assistant"),
        _says("n" * 40_000, role="assistant"),
    )

    result = plan_context(
        window=window,
        safety_rules=RULES,
        personality=PERSONALITY,
        tool_schemas=(),
        history=history,
        threshold=THRESHOLD,
    )

    assert result.fits is False
    assert result.compacted is None
    assert result.messages == tuple(item.message for item in history)


def test_a_round_that_does_not_fit_compacts_whatever_it_saves() -> None:
    """The minimum gain keeps a round that fits from paying a summarizer for
    a few messages. A round that does not fit has no such choice."""
    window = ContextWindow(1_200, reserved_output_tokens=200)
    history = _stored(
        _says("the task, stated at length: " + "t" * 600),
        *(_says(f"round {index}: " + "w" * 600, role="assistant") for index in range(8)),
        _says("what is left?"),
    )

    result = plan_context(
        window=window,
        safety_rules=RULES,
        personality=PERSONALITY,
        tool_schemas=(),
        history=history,
        threshold=THRESHOLD,
    )

    assert result.fits is True
    assert result.compacted is not None


def test_the_minimum_gain_shrinks_with_the_window() -> None:
    """4,096 tokens is most of a small window; a round that fits but is over
    its trigger may compact for a sixteenth of the allowance."""
    window = ContextWindow(13_568, reserved_output_tokens=4_096)
    history = _stored(
        *(
            _says(_words(700, f"turn {index}"), role="user" if index % 2 == 0 else "assistant")
            for index in range(12)
        ),
        _says("and?"),
    )
    raw = plan_context(
        window=ContextWindow(100_000_000, reserved_output_tokens=1_000),
        safety_rules=RULES,
        personality=PERSONALITY,
        tool_schemas=(),
        history=history,
    ).input_estimate
    assert window.input_allowance * THRESHOLD < raw <= window.input_allowance

    result = plan_context(
        window=window,
        safety_rules=RULES,
        personality=PERSONALITY,
        tool_schemas=(),
        history=history,
        threshold=THRESHOLD,
    )

    assert result.compacted is not None
    assert result.compacted.freed_estimate < MIN_COMPACTION_GAIN_TOKENS


def test_a_stub_of_a_capped_result_states_the_original_length() -> None:
    """A result capped on entry and later stubbed must still say how long the
    output really was, not how long the capped copy was."""
    window = ContextWindow(4_000, reserved_output_tokens=500)
    history = _stored(
        _says("run it"),
        _called("c1"),
        _answered("c1", "z" * 40_000),
        _says("and again"),
    )

    result = plan_context(
        window=window,
        safety_rules=RULES,
        personality=PERSONALITY,
        tool_schemas=(),
        history=history,
        threshold=THRESHOLD,
    )

    stub = result.messages[2].blocks[0]
    assert isinstance(stub, ToolResultBlock)
    assert "40000 characters in full" in stub.output


def test_a_bigger_window_never_pauses_where_a_smaller_one_fits() -> None:
    """The tail takes whole messages, so the one crossing the target could be
    a result bigger than the room; it stays outside the tail instead, and is
    compacted. Found by sweeping windows: 460 and 740 fit, 520–720 paused."""
    history = _stored(
        _says("start"),
        _called("c1"),
        _answered("c1", "x" * 4_000),
        _says("and now the thing I actually want" + "y" * 200),
    )
    for size in range(400, 1_000, 20):
        result = plan_context(
            window=ContextWindow(context_window=size, reserved_output_tokens=0),
            safety_rules=RULES,
            personality=PERSONALITY,
            tool_schemas=(),
            history=history,
            threshold=THRESHOLD,
        )
        assert result.fits, size
