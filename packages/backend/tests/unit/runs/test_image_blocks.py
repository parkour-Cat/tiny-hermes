"""An image in a conversation, from storage to the wire.

A person sends a photo. Until now the platform said it could only read
text — honestly, but the answer to "why" was that nothing carried an image
anywhere: not the transcript, not the provider request, not the token
estimate.

The block is where that starts. It is deliberately a *reference* rather
than the bytes: a transcript row holds a pointer, and the bytes live where
artifacts live. Putting base64 in `session_messages` would put a megabyte
into every context estimate and every `content::text` read.
"""

import json

from tiny_hermes.runs.domain.models import (
    CanonicalMessage,
    ImageBlock,
    TextBlock,
    message_from_document,
)


def test_an_image_block_round_trips_through_storage() -> None:
    turn = CanonicalMessage(
        role="user",
        blocks=(
            TextBlock(text="这张图里是什么?"),
            ImageBlock(artifact_id="a1", media_type="image/png"),
        ),
    )

    restored = message_from_document(json.loads(json.dumps(turn.document())))

    assert restored.blocks == turn.blocks


def test_an_image_is_not_part_of_what_was_said() -> None:
    """`text` is what a transcript shows, what a Feishu reply quotes, and
    what `chat-web` uses to decide whether a turn is worth rendering. An
    image is not words — and an artifact id leaking into any of those is a
    pointer shown to a person as if it were speech."""
    turn = CanonicalMessage(
        role="user",
        blocks=(
            TextBlock(text="这张图里是什么?"),
            ImageBlock(artifact_id="a1", media_type="image/png"),
        ),
    )

    assert turn.text == "这张图里是什么?"


def test_a_turn_may_be_an_image_alone() -> None:
    """People send a photo with no caption constantly. A message needs at
    least one block, and an image is one — requiring text beside it would
    refuse the most ordinary case there is."""
    turn = CanonicalMessage(
        role="user", blocks=(ImageBlock(artifact_id="a1", media_type="image/jpeg"),)
    )

    assert turn.blocks[0].artifact_id == "a1"  # pyright: ignore[reportAttributeAccessIssue]


def test_an_unknown_media_type_is_kept_rather_than_guessed() -> None:
    """The type comes from the channel that received the file. Guessing it
    from bytes here would put a second, disagreeing answer next to the one
    the sender's platform already gave."""
    turn = CanonicalMessage(
        role="user",
        blocks=(ImageBlock(artifact_id="a1", media_type="image/heic"),),
    )

    restored = message_from_document(json.loads(json.dumps(turn.document())))

    assert restored.blocks[0].media_type == "image/heic"  # pyright: ignore[reportAttributeAccessIssue]
