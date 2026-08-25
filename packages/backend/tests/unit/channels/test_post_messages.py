"""A rich-text message, which is what a photo with a caption actually is.

The first real attempt to send a picture with a question attached came back
「我暂时还处理不了富文本消息」. Feishu classifies image-plus-text as `post`,
not `image` — so the type this build had just learned to read was the one
people almost never send on its own.

`post` content is language-keyed and nested two deep: `{lang: {title,
content: [[element, ...], ...]}}`, with images in `tag: "img"` paragraphs of
their own. All of it is the sender's message, so all of it is read: the
title, every text run, and every image.
"""

import json
from typing import Any

import pytest
from tiny_hermes.channels.domain.events import MalformedChannelEvent
from tiny_hermes.channels.domain.feishu import event_from_envelope


def _post(body: dict[str, Any], message_id: str | None = "om_post_1") -> dict[str, Any]:
    message: dict[str, Any] = {"message_type": "post", "content": json.dumps(body)}
    if message_id is not None:
        message["message_id"] = message_id
    return {
        "header": {"event_id": "om_1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_zhang"}},
            "message": message,
        },
    }


def _zh(*paragraphs: list[dict[str, Any]], title: str = "") -> dict[str, Any]:
    return {"zh_cn": {"title": title, "content": list(paragraphs)}}


def test_a_caption_and_a_picture_both_survive() -> None:
    """The case that failed in front of a person."""
    event = event_from_envelope(
        _post(
            _zh(
                [{"tag": "img", "image_key": "img_v2_abc"}],
                [{"tag": "text", "text": "图里是什么"}],
            )
        )
    )

    assert event.text == "图里是什么"
    assert len(event.images) == 1
    assert event.images[0].file_key == "img_v2_abc"
    assert event.images[0].message_id == "om_post_1"


def test_text_across_paragraphs_is_joined() -> None:
    """Each inner array is a line. Concatenating them without a break would
    run two sentences together into one the sender never wrote."""
    event = event_from_envelope(
        _post(
            _zh(
                [{"tag": "text", "text": "第一行"}],
                [{"tag": "text", "text": "第二行"}],
            )
        )
    )

    assert event.text == "第一行\n第二行"


def test_runs_within_one_paragraph_are_not_broken_apart() -> None:
    """Feishu splits a styled line into several runs. They are one line to
    the person who typed them."""
    event = event_from_envelope(
        _post(_zh([{"tag": "text", "text": "重要"}, {"tag": "text", "text": "的事"}]))
    )

    assert event.text == "重要的事"


def test_the_title_is_part_of_what_was_said() -> None:
    """A person who filled it in meant it to be read."""
    event = event_from_envelope(
        _post(_zh([{"tag": "text", "text": "正文"}], title="标题"))
    )

    assert "标题" in event.text
    assert "正文" in event.text


def test_a_link_contributes_its_text() -> None:
    """An `a` element carries the words a person saw. Dropping it would
    silently remove part of the sentence."""
    event = event_from_envelope(
        _post(_zh([{"tag": "a", "text": "这个文档", "href": "https://x/y"}]))
    )

    assert "这个文档" in event.text


def test_several_pictures_all_survive() -> None:
    event = event_from_envelope(
        _post(
            _zh(
                [{"tag": "img", "image_key": "img_1"}],
                [{"tag": "img", "image_key": "img_2"}],
            )
        )
    )

    assert [picture.file_key for picture in event.images] == ["img_1", "img_2"]


def test_an_unknown_element_is_skipped_rather_than_refused() -> None:
    """A divider or an emoji is not content this build can render, and it is
    also not a reason to refuse a message whose words came through fine."""
    event = event_from_envelope(
        _post(_zh([{"tag": "hr"}, {"tag": "text", "text": "还在"}]))
    )

    assert event.text == "还在"


def test_a_post_with_no_readable_content_is_refused() -> None:
    """Nothing to act on. Creating a Run from it would hand the Agent an
    empty turn and charge somebody for the round."""
    with pytest.raises(MalformedChannelEvent):
        event_from_envelope(_post(_zh([{"tag": "hr"}])))


def test_a_post_carrying_an_image_needs_its_message_id() -> None:
    """Same rule as a plain image: half an address cannot be fetched."""
    with pytest.raises(MalformedChannelEvent):
        event_from_envelope(
            _post(_zh([{"tag": "img", "image_key": "img_1"}]), message_id=None)
        )


def test_a_post_whose_language_key_differs_still_reads() -> None:
    """The event carries whichever locale the sender's client used. Looking
    for `zh_cn` alone would drop an English speaker's message entirely."""
    event = event_from_envelope(
        _post({"en_us": {"title": "", "content": [[{"tag": "text", "text": "hello"}]]}})
    )

    assert event.text == "hello"
