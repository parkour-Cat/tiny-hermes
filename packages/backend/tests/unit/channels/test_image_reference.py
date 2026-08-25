"""Turning a channel's image reference into bytes the model can be shown.

The reference travels in the transcript; the bytes are fetched when a round
is built. That split is why the reference has to say *everything* needed to
fetch it — there is no lookup table on the other side, and a Run replayed
weeks later resolves the same string.

`feishu:{message_id}:{file_key}` is that string. Parsed rather than
regex-matched loosely, because a malformed one has to fail loudly: the
alternative is a request that reaches DeepSeek with a question about a
picture and no picture.
"""

import pytest
from tiny_hermes.channels.domain.image_reference import (
    MalformedImageReference,
    feishu_reference,
    parse_reference,
)


def test_a_reference_round_trips() -> None:
    made = feishu_reference(message_id="om_1", file_key="img_v2_abc")

    parsed = parse_reference(made)

    assert parsed.message_id == "om_1"
    assert parsed.file_key == "img_v2_abc"


def test_the_reference_names_its_surface() -> None:
    """A bare pair of ids would be unresolvable the day a second channel
    exists — and this platform's whole channel design assumes there will be
    one."""
    assert feishu_reference(message_id="om_1", file_key="k").startswith("feishu:")


def test_a_reference_from_another_surface_is_refused() -> None:
    with pytest.raises(MalformedImageReference):
        parse_reference("slack:C123:F456")


@pytest.mark.parametrize(
    "broken",
    ["feishu:om_1", "feishu:", "feishu::k", "feishu:om_1:", "", "om_1:k"],
)
def test_a_malformed_reference_is_refused_rather_than_guessed(broken: str) -> None:
    """Loudly, because the alternative is a request that reaches the model
    with a question about a picture and no picture attached."""
    with pytest.raises(MalformedImageReference):
        parse_reference(broken)


def test_a_file_key_containing_a_colon_survives() -> None:
    """The key is the vendor's, and this platform does not get to assume
    what is in it. Splitting from the left with a bound keeps a colon in
    the key from silently truncating the reference."""
    made = feishu_reference(message_id="om_1", file_key="img:v2:abc")

    assert parse_reference(made).file_key == "img:v2:abc"
