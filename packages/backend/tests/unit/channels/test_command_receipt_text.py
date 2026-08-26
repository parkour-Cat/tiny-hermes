"""一条命令的结果，说给发它的人听。

`CommandReceipt`是存成的事实；这里是把事实变成一句人话的地方。忙、没得撤、
撤成了——都是不同的话，混成一句「命令已处理」就又是一次「写进去了没人够得着」
的翻版，只是这次卡在措辞而不是查询。

`test_the_blocked_card_names_the_way_out` 顺带钉住 §935：这个 build 不渲染
交互按钮（原因见 `feishu_card.py` 191 行附近），阻塞卡片给的「新建会话」入口
只能是文字里提到的 `/new` 命令本身。
"""

from tiny_hermes.channels.domain.command_receipt import CommandReceipt
from tiny_hermes.channels.infrastructure.feishu_card import command_receipt_text


def test_a_finished_undo_says_how_much_and_echoes_the_text() -> None:
    text = command_receipt_text(
        CommandReceipt("undo", "done", 2, 1, "图里是什么", None)
    )

    assert "1" in text
    assert "图里是什么" in text


def test_a_busy_receipt_says_which_kind_of_busy() -> None:
    running = command_receipt_text(CommandReceipt("undo", "busy", 0, 0, "", "running"))
    queued = command_receipt_text(CommandReceipt("undo", "busy", 0, 0, "", "queued"))

    assert running != queued


def test_nothing_to_undo_says_so_rather_than_a_count_of_zero() -> None:
    """`0 轮、0 条` reads like a bug report, not an answer to the person who
    just tried to undo something that was not there."""
    text = command_receipt_text(
        CommandReceipt("undo", "nothing", 0, 0, "", None)
    )

    assert "没有" in text


def test_a_long_echo_is_truncated_rather_than_sent_in_full() -> None:
    """照上游 Hermes 的 200 字符：够认出是哪条消息，又不至于把一条长提示整段
    抄回聊天窗口。"""
    long_text = "字" * 500
    text = command_receipt_text(
        CommandReceipt("undo", "done", 1, 1, long_text, None)
    )

    assert len(text) < 500


def test_the_blocked_card_names_the_way_out() -> None:
    from tiny_hermes.channels.domain.blocked import BlockedNotice
    from tiny_hermes.channels.infrastructure.feishu_card import blocked_card

    card = blocked_card(
        BlockedNotice(None, "paused", None, None, 2, ("cancel",))
    )

    assert "/new" in str(card)
