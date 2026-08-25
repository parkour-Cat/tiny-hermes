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

    only = turn.blocks[0]
    assert isinstance(only, ImageBlock)
    assert only.artifact_id == "a1"


def test_an_unknown_media_type_is_kept_rather_than_guessed() -> None:
    """The type comes from the channel that received the file. Guessing it
    from bytes here would put a second, disagreeing answer next to the one
    the sender's platform already gave."""
    turn = CanonicalMessage(
        role="user",
        blocks=(ImageBlock(artifact_id="a1", media_type="image/heic"),),
    )

    restored = message_from_document(json.loads(json.dumps(turn.document())))

    only = restored.blocks[0]
    assert isinstance(only, ImageBlock)
    assert only.media_type == "image/heic"


def test_every_block_type_is_estimated() -> None:
    """The guard the `else` branch cannot provide.

    `_message_estimate` walks block types and its last branch is a catch-all.
    Every block type added so far has walked into it — `ReasoningBlock` did,
    `ImageBlock` did — and both were caught only because they happened to
    lack the attribute that branch read. A future block that happens to have
    one would be estimated as something it is not, silently.

    So the union itself is the test. A new member with no deliberate cost
    fails here, at the moment it is declared, rather than in whatever Run
    first exceeds a window it was told it fit inside.
    """
    from typing import get_args

    from tiny_hermes.runs.domain.context_budget import (
        _message_estimate,  # pyright: ignore[reportPrivateUsage]
    )
    from tiny_hermes.runs.domain.models import (
        Block,
        ReasoningBlock,
        ToolCallBlock,
        ToolResultBlock,
    )

    samples: dict[type, object] = {
        TextBlock: TextBlock(text="hello"),
        ReasoningBlock: ReasoningBlock(text="thinking"),
        ImageBlock: ImageBlock(artifact_id="a1", media_type="image/png"),
        ToolCallBlock: ToolCallBlock(call_id="c1", name="file.read", arguments={}),
        ToolResultBlock: ToolResultBlock(
            call_id="c1", output="out", exit_code=0, failed=False
        ),
    }
    declared = set(get_args(Block))

    assert declared == set(samples), (
        "a block type was added or removed without deciding what it costs: "
        f"{declared ^ set(samples)}"
    )
    for kind, block in samples.items():
        turn = CanonicalMessage(role="user", blocks=(block,))  # pyright: ignore[reportArgumentType]
        assert _message_estimate(turn, None) > 0, f"{kind.__name__} costs nothing"
