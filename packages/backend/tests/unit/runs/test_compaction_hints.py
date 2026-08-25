"""What a compacted range leaves behind for a search to find.

Compaction replaces old turns with one summary. That summary says how many
messages there were and which tools ran — it does not say what any of it
was **about**. So the model knows it has forgotten something and has no way
to ask for it back: `session.search` (§14.3) needs a word to search for,
and the one place that word could have come from is the text that was just
removed from its context.

The hints are extracted, not generated. `_summarize`'s docstring gives the
reason and it is not stylistic: a deterministic summary can be asserted
against, costs nothing to recompute, and — the load-bearing one — lets a
replayed Run rebuild the same context. This platform recovers interrupted
Runs (`SchedulerRuntime._recover_interrupted`), so a summary that came back
different on replay would change what the model saw between attempts.
"""

# `_summarize` is private and asserted on directly, which pyright flags. The
# alternative is reaching it through `plan_context` with a hand-built window,
# and that tests the planner's arithmetic rather than the sentence a model
# ends up reading — which is the thing these tests are about.
from tiny_hermes.runs.domain.context_budget import (
    _summarize,  # pyright: ignore[reportPrivateUsage]
    compaction_hints,
)
from tiny_hermes.runs.domain.models import (
    CanonicalMessage,
    StoredMessage,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)


def _said(sequence: int, text: str, role: str = "user") -> StoredMessage:
    from uuid import uuid4

    return StoredMessage(
        id=uuid4(),
        sequence=sequence,
        message=CanonicalMessage(role=role, blocks=(TextBlock(text=text),)),  # pyright: ignore[reportArgumentType]
    )


def test_a_repeated_english_term_becomes_a_hint() -> None:
    hints = compaction_hints(
        [
            _said(1, "the pelican rollout is on Tuesday"),
            _said(2, "pelican needs the new dashboard first"),
        ]
    )

    assert "pelican" in hints


def test_a_repeated_chinese_term_becomes_a_hint() -> None:
    """The case the whole feature is for. Chinese has no spaces, so a naive
    word split finds nothing at all — and this platform's conversations are
    in Chinese."""
    hints = compaction_hints(
        [
            _said(1, "鹈鹕项目的发布定在下周二"),
            _said(2, "鹈鹕项目还需要新的看板"),
        ]
    )

    assert any("鹈鹕" in hint for hint in hints)


def test_a_term_said_once_is_not_a_hint() -> None:
    """A hint list that includes everything is an index of the conversation,
    which is the thing compaction just spent tokens removing."""
    hints = compaction_hints(
        [
            _said(1, "the pelican rollout is on Tuesday"),
            _said(2, "the pelican rollout is on Tuesday"),
            _said(3, "an incidental mention of aardvarks"),
        ]
    )

    assert "aardvarks" not in hints


def test_the_hint_list_is_bounded() -> None:
    """It rides in the summary, which exists to save context. A hint list
    that grew with the conversation would give back what compaction took."""
    many = [_said(i, f"topic{i} topic{i} topic{i}") for i in range(1, 200)]

    assert len(compaction_hints(many)) <= 12


def test_hints_are_the_same_for_the_same_input() -> None:
    """The property that decides this is extraction and not a model call.
    A Run recovered by the Scheduler replays this exact computation, and a
    different answer would change what the model saw between attempts."""
    covered = [_said(1, "鹈鹕项目的发布"), _said(2, "鹈鹕项目的看板")]

    assert compaction_hints(covered) == compaction_hints(covered)


def test_tool_names_are_not_repeated_as_hints() -> None:
    """The summary already lists them by name. Repeating them would crowd
    out the words that are only findable here."""
    from uuid import uuid4

    covered = [
        StoredMessage(
            id=uuid4(),
            sequence=1,
            message=CanonicalMessage(  # pyright: ignore[reportArgumentType]
                role="assistant",
                blocks=(
                    ToolCallBlock(call_id="c1", name="file.read", arguments={}),
                ),
            ),
        ),
        StoredMessage(
            id=uuid4(),
            sequence=2,
            message=CanonicalMessage(  # pyright: ignore[reportArgumentType]
                role="tool",
                blocks=(ToolResultBlock(
                        call_id="c1",
                        output="file.read file.read",
                        exit_code=0,
                        failed=False,
                    ),),
            ),
        ),
    ]

    assert "file.read" not in compaction_hints(covered)


def test_nothing_worth_hinting_yields_nothing() -> None:
    """Empty rather than a filler line. A summary that always ends with a
    hint section teaches the reader to skip it."""
    assert compaction_hints([_said(1, "ok")]) == ()


def test_the_summary_carries_the_hints_it_computed() -> None:
    """The step that decides whether any of this is reachable.

    A hint list computed and left in a variable is this repository's
    signature failure — written, correct, and read by nobody. It has to
    appear in the text the model actually receives.
    """

    summary = _summarize(
        [
            _said(1, "鹈鹕项目的发布定在下周二"),
            _said(2, "鹈鹕项目还需要新的看板"),
        ]
    )

    assert "鹈鹕" in summary


def test_a_summary_with_no_hints_says_nothing_about_them() -> None:

    summary = _summarize([_said(1, "ok")])

    assert "Topics" not in summary


def test_the_summary_tells_the_model_it_can_search_for_them() -> None:
    """A bare word list is a puzzle. The sentence around it is what turns
    the hints into something the model knows to act on — `session.search`
    is the tool, and the summary is the only place it learns these terms
    exist at all."""

    summary = _summarize(
        [
            _said(1, "鹈鹕项目的发布定在下周二"),
            _said(2, "鹈鹕项目还需要新的看板"),
        ]
    )

    assert "search" in summary.lower()


def test_hints_are_dropped_before_compaction_is_allowed_to_fail() -> None:
    """The priority when both cannot fit.

    Hints cost tokens, and a summary carrying them can be the difference
    between compaction fitting and not — at which point §7.4.2 pauses the
    Run with `context_overflow` and the person gets nothing at all. Being
    able to search for a topic is worth less than the conversation
    continuing, so it is the half that gets dropped.

    Found by an existing test rather than by design: adding hints made
    `test_the_summary_says_what_it_replaced_and_where_to_find_it` stop
    compacting altogether.
    """

    covered = [
        _said(1, "鹈鹕项目的发布定在下周二"),
        _said(2, "鹈鹕项目还需要新的看板"),
    ]

    with_hints = _summarize(covered)
    without = _summarize(covered, with_hints=False)

    assert "鹈鹕" in with_hints
    assert "鹈鹕" not in without
    assert len(without) < len(with_hints)
