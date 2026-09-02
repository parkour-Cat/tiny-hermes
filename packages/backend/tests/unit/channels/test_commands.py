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


def test_compact_is_a_command_and_takes_no_argument() -> None:
    """`/compact` 让用户自己决定什么时候压缩，不必等阈值。

    不收参数：`/undo` 的参数是「撤几轮」，语义清楚；压缩没有对应的自然参数
    ——「压到多少」既不是用户能判断的，也不是这个平台愿意让他指定的。多带一个词
    就不认，和 `/new` 同一条规矩。
    """
    assert parse("/compact") == ChatCommand(name=CommandName.COMPACT)
    assert parse("/COMPACT") == ChatCommand(name=CommandName.COMPACT)
    assert parse("/compact 3") is None
    assert parse("/compact now") is None


def test_a_compact_with_an_image_is_not_a_command() -> None:
    """和 `/undo` 同一条理由：带附件的消息几乎总是在说别的事。"""
    assert parse("/compact", has_images=True) is None
