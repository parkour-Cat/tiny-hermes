"""A Feishu file message, read as an attachment the Run can work on (§1.27, v2.27)."""

import json
from typing import Any

import pytest
from tiny_hermes.channels.domain.events import MalformedChannelEvent
from tiny_hermes.channels.domain.feishu import event_from_envelope
from tiny_hermes.channels.domain.files import inbox_name


def _envelope(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "header": {"event_id": "om_1"},
        "event": {"sender": {"sender_id": {"open_id": "ou_zhang"}}, "message": message},
    }


def _file(**overrides: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_type": "file",
        "message_id": "om_msg_1",
        "content": json.dumps({"file_key": "file_v3_abc", "file_name": "销售明细.xlsx"}),
    }
    message.update(overrides)
    return message


def test_a_file_message_is_read_with_what_its_download_needs() -> None:
    event = event_from_envelope(_envelope(_file()))

    [attached] = event.files
    assert (attached.message_id, attached.file_key, attached.name) == (
        "om_msg_1",
        "file_v3_abc",
        "销售明细.xlsx",
    )
    assert event.text == "" and event.images == ()


@pytest.mark.parametrize(
    "broken",
    [{"message_id": None}, {"content": json.dumps({"file_name": "a.xlsx"})}],
)
def test_a_file_message_missing_its_id_or_key_is_refused(broken: dict[str, Any]) -> None:
    with pytest.raises(MalformedChannelEvent):
        event_from_envelope(_envelope(_file(**broken)))


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("销售明细.xlsx", "销售明细.xlsx"),
        ("../../etc/passwd", "passwd"),
        ("a/b\\c.docx", "c.docx"),
        (".hidden", "hidden"),
        ("tab\there\n.pdf", "tab_here_.pdf"),
        ("", "file"),
        ("..", "file"),
        ("x" * 300 + ".xlsx", "x" * 115 + ".xlsx"),
    ],
)
def test_a_file_name_is_made_safe_for_the_inbox(given: str, kept: str) -> None:
    """The sender chooses the name; the workspace must not be steered by it."""
    assert inbox_name(given) == kept
