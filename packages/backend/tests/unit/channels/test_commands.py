"""命令解析：认得出该认的，认不出不该认的。

第二类比第一类重要。渠道里一条 `/usr/local/bin` 或一句玩笑被当成命令吞掉，
用户看到的是消息石沉大海——比命令不好用糟得多。
"""

import pytest
from tiny_hermes.channels.domain.commands import ChatCommand, CommandName, parse


@pytest.mark.parametrize(
    "text",
    ["/undo", "/UNDO", "  /undo  ", "/Undo"],
)
def test_undo_is_recognised_whatever_the_case_or_padding(text: str) -> None:
    assert parse(text) == ChatCommand(name=CommandName.UNDO, turns=1)


def test_undo_takes_a_turn_count() -> None:
    assert parse("/undo 3") == ChatCommand(name=CommandName.UNDO, turns=3)


@pytest.mark.parametrize("text", ["/new", "/reset", "/NEW"])
def test_new_and_its_alias(text: str) -> None:
    assert parse(text) == ChatCommand(name=CommandName.NEW, turns=1)


@pytest.mark.parametrize(
    "text",
    [
        "/undoing",
        "/undo 顺便帮我看看这个",
        "/usr/local/bin",
        "/newsletter",
        "undo",
        "",
        "/undo -1",
        "/undo 0",
        "/undo abc",
        "/new 3",
        # `'²'.isdigit()` is True while `int('²')` raises. A `ValueError` out
        # of `parse` leaves the webhook returning 500, the claim rolled back,
        # and Feishu redelivering the same event for six hours — so this one
        # is not merely "unrecognised", it is the case that must not crash.
        "/undo ²",
        "/undo ٣",
    ],
)
def test_what_must_not_be_swallowed(text: str) -> None:
    assert parse(text) is None


def test_a_message_carrying_an_image_is_never_a_command() -> None:
    assert parse("/undo", has_images=True) is None
