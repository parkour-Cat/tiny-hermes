"""An attachment in a transcript, by reference (§1.27, v2.27)."""

from tiny_hermes.runs.domain.models import (
    CanonicalMessage,
    FileBlock,
    TextBlock,
    message_from_document,
)


def test_a_file_block_survives_being_stored_and_read_back() -> None:
    message = CanonicalMessage(
        "user", (TextBlock(text="附件"), FileBlock(reference="feishu:om:fk", name="a.xlsx"))
    )

    assert message_from_document(message.document()) == message


def test_a_file_is_not_text_the_model_is_sent() -> None:
    """The model reads the note beside it; the reference is the Worker's."""
    message = CanonicalMessage("user", (FileBlock(reference="feishu:om:fk", name="a.xlsx"),))

    assert message.text == ""
