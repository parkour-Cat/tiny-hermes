"""A message becomes a list of blocks, and phase-3A rows keep reading.

Phase 3A left `CanonicalMessage.text` a single string with a docstring saying
the widening belonged to the slice that had a producer. Tool calls are that
producer, so this is that slice.

The compatibility claim is the one that matters and it is asserted against a
literal document, because "rows written before this change still read" is a
promise about bytes already in a database — not about a shape that happens to
round-trip through today's code.
"""

from typing import Any

import pytest
from tiny_hermes.runs.domain.models import (
    CanonicalMessage,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    message_from_document,
)

#: Exactly what phase 3A wrote into `session_messages.content`. Copied from a
#: row rather than produced by calling `document()`, so a change to `document()`
#: cannot quietly change what this is checked against.
PHASE_3A_DOCUMENT: dict[str, Any] = {
    "role": "assistant",
    "parts": [{"type": "text", "text": "Blue."}],
}


def test_a_text_message_still_produces_the_phase_3a_document() -> None:
    message = CanonicalMessage(role="assistant", blocks=(TextBlock(text="Blue."),))
    assert message.document() == PHASE_3A_DOCUMENT


def test_a_phase_3a_row_reads_back_as_one_text_block() -> None:
    message = message_from_document(PHASE_3A_DOCUMENT)
    assert message.role == "assistant"
    assert message.blocks == (TextBlock(text="Blue."),)
    assert message.text == "Blue."


def test_text_is_still_available_because_most_readers_only_want_that() -> None:
    """A convenience, and deliberately lossy.

    Everything that renders a conversation to a person wants the words. The
    accessor concatenates text blocks and drops the rest, which is right for
    display and wrong for anything reconstructing a request — so the provider
    adapter walks `blocks` instead.
    """
    message = CanonicalMessage(
        role="assistant",
        blocks=(
            TextBlock(text="Let me check."),
            ToolCallBlock(call_id="c1", name="shell.exec", arguments={"command": "ls"}),
        ),
    )
    assert message.text == "Let me check."


def test_a_tool_call_block_round_trips() -> None:
    message = CanonicalMessage(
        role="assistant",
        blocks=(ToolCallBlock(call_id="c1", name="shell.exec", arguments={"command": "ls"}),),
    )
    document = message.document()
    assert document == {
        "role": "assistant",
        "parts": [
            {
                "type": "tool_call",
                "call_id": "c1",
                "name": "shell.exec",
                "arguments": {"command": "ls"},
            }
        ],
    }
    assert message_from_document(document) == message


def test_a_tool_result_block_round_trips() -> None:
    message = CanonicalMessage(
        role="tool",
        blocks=(
            ToolResultBlock(call_id="c1", output="a.txt\n", exit_code=0, failed=False),
        ),
    )
    assert message_from_document(message.document()) == message


def test_a_message_may_carry_text_and_a_call_together() -> None:
    """Which is what a model that explains itself before acting produces."""
    message = CanonicalMessage(
        role="assistant",
        blocks=(
            TextBlock(text="Listing the directory."),
            ToolCallBlock(call_id="c1", name="shell.exec", arguments={"command": "ls"}),
        ),
    )
    assert message_from_document(message.document()) == message


def test_a_message_with_no_blocks_is_refused() -> None:
    """An empty turn is not a turn. Storing one would put a row in the
    transcript that means nothing and that the next round has to skip."""
    with pytest.raises(ValueError, match="at least one block"):
        CanonicalMessage(role="user", blocks=())


def test_a_tool_result_must_name_a_call() -> None:
    with pytest.raises(ValueError, match="call_id"):
        ToolResultBlock(call_id="", output="", exit_code=0, failed=False)


def test_a_tool_call_must_have_a_name_and_an_id() -> None:
    with pytest.raises(ValueError):
        ToolCallBlock(call_id="c1", name="", arguments={})
    with pytest.raises(ValueError):
        ToolCallBlock(call_id="", name="shell.exec", arguments={})


def test_an_unknown_part_type_is_dropped_rather_than_guessed_at() -> None:
    """A row written by a later version, read by this one.

    Dropping is right and inventing is not: a block this version does not
    understand cannot be rendered or replayed honestly, and a placeholder would
    put words in the Agent's mouth.
    """
    message = message_from_document(
        {
            "role": "assistant",
            "parts": [
                {"type": "text", "text": "kept"},
                {"type": "hologram", "url": "https://example.com/x.png"},
            ],
        }
    )
    assert message.blocks == (TextBlock(text="kept"),)


def test_a_document_with_no_readable_part_becomes_an_empty_text_block() -> None:
    """Rather than raising inside a Worker mid-slice.

    A transcript row the platform cannot read is a bad row, not a reason to
    fail a Run that has nothing to do with it.
    """
    message = message_from_document({"role": "user", "parts": [{"type": "hologram"}]})
    assert message.blocks == (TextBlock(text=""),)


@pytest.mark.parametrize("role", ["user", "assistant", "tool"])
def test_the_three_roles_a_transcript_can_hold(role: str) -> None:
    message = CanonicalMessage(role=role, blocks=(TextBlock(text="x"),))  # type: ignore[arg-type]
    assert message_from_document(message.document()).role == role
