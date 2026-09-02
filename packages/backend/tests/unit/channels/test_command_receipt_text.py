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


def test_new_on_an_empty_session_still_confirms_a_fresh_conversation() -> None:
    """`/new` 的「没得撤」不是「你的命令没做成」——它是「你已经在一段新对话里
    了」。回一句 `/undo` 的「没有可撤的内容」会让人以为 `/new` 失败了，于是
    再发一次。
    """
    text = command_receipt_text(CommandReceipt("new", "nothing", 0, 0, "", None))

    assert "新对话" in text
    assert "没有可撤的内容。" != text


def test_a_parked_head_and_a_running_one_are_not_the_same_sentence() -> None:
    """停住的队首是 `/new` 唯一能救的那种忙。`/undo` 撞上它时收到的那句话必须
    指得出这条路——阻塞卡片就是这么写给同一个人的。
    """
    parked = command_receipt_text(CommandReceipt("undo", "busy", 0, 0, "", "parked"))
    running = command_receipt_text(CommandReceipt("undo", "busy", 0, 0, "", "running"))

    assert parked != running
    assert "/new" in parked


def test_a_cancel_that_failed_does_not_read_as_a_fresh_conversation() -> None:
    """`/new` 取消不掉那个停住的 Run 时什么都没撤。用户必须知道这一点，否则
    他会以为自己在一段新对话里，而那个 Run 还能醒进来。
    """
    text = command_receipt_text(
        CommandReceipt("new", "busy", 0, 0, "", "cancel_failed")
    )

    assert "新对话" not in text


def test_new_says_how_many_unfinished_runs_it_ended() -> None:
    """结束掉的 Run 不会再有答复。不说出来，用户只会发现自己有条消息石沉大海
    ——而他刚刚被告知这是一段全新的对话，正好没有理由去找。
    """
    text = command_receipt_text(
        CommandReceipt("new", "done", 4, 2, "", None, runs_ended=2)
    )

    assert "2" in text
    assert "新对话" in text


def test_new_that_ended_nothing_does_not_mention_a_count() -> None:
    """没结束任何 Run 时不该冒出「结束了 0 个任务」这种话。"""
    text = command_receipt_text(
        CommandReceipt("new", "done", 4, 2, "", None, runs_ended=0)
    )

    assert "0 个" not in text


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


def test_a_compact_receipt_says_when_it_takes_effect_not_that_it_already_did() -> None:
    """回执不能承诺一件还没发生的事。

    压缩要一次模型调用，而入站路径不做外部调用（和「被挡通知在这里记下、不在这里
    发」同一条理由：一次投递不该依赖模型端点可达）。所以命令做的是打标记，压缩在
    下一轮真正开始前发生。

    措辞必须让这件事看得出来。说「已压缩」是假话；说「下次会压缩」又是个不一定
    兑现的承诺——历史不够长时没有可压缩的内容。所以说的是条件句。
    """
    said = command_receipt_text(
        CommandReceipt(
            command="compact",
            outcome="done",
            messages=0,
            turns=0,
            echoed_text="",
            busy_reason=None,
        )
    )
    assert "已压缩" not in said
    assert "下" in said or "接下来" in said


def test_a_compact_receipt_on_a_conversation_with_nothing_to_compact_says_so() -> None:
    """和 `/new` 的 `nothing` 一样不能和 `/undo` 合并成一句。

    对 `/compact`，「没什么可压」意味着这段对话还短——那是一句让人放心的话，
    不是一句失败。"""
    said = command_receipt_text(
        CommandReceipt(
            command="compact",
            outcome="nothing",
            messages=0,
            turns=0,
            echoed_text="",
            busy_reason=None,
        )
    )
    assert "没有可撤" not in said
