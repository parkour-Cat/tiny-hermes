"""A turn the platform wrote, told apart from one a person typed.

Design §4.5. `continue` has to say something to the next round, and the kernel
has one role for "what the agent is being asked" — `user`. That leaves a
transcript in which the platform's instruction is indistinguishable from
something a human sent, which is wrong in the two places it matters: the
console shows it as the user's words, and the person reading it later cannot
tell why the Run kept going.

So the marker is on the message, and it is absent by default — the stored
document for every row written before this slice is unchanged, which is the
same promise `test_message_blocks.py` makes about phase 3A rows.
"""

from typing import Any

from tiny_hermes.runs.domain.models import (
    CanonicalMessage,
    TextBlock,
    message_from_document,
)

HUMAN_DOCUMENT: dict[str, Any] = {
    "role": "user",
    "parts": [{"type": "text", "text": "write the report"}],
}


def test_an_ordinary_turn_carries_no_marker_and_no_new_key() -> None:
    message = CanonicalMessage(role="user", blocks=(TextBlock(text="write the report"),))

    assert message.document() == HUMAN_DOCUMENT
    assert message.author is None


def test_a_row_written_before_this_slice_reads_back_as_a_human_turn() -> None:
    assert message_from_document(HUMAN_DOCUMENT).author is None


def test_a_platform_turn_says_so_in_the_document_and_reads_back() -> None:
    message = CanonicalMessage(
        role="user",
        blocks=(TextBlock(text="The task is not finished."),),
        author="platform",
    )

    document = message.document()

    assert document["author"] == "platform"
    assert message_from_document(document).author == "platform"


def test_an_author_nobody_recognizes_is_read_as_a_human_turn() -> None:
    """The safe direction. Claiming a turn is the platform's when this version
    cannot tell would put words in someone else's mouth."""
    document = {**HUMAN_DOCUMENT, "author": "some-later-version"}

    assert message_from_document(document).author is None
