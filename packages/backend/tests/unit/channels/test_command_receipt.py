"""回执存的是事实，不是渲染好的句子。

和 BlockedNotice 同一个理由：入站那一刻是唯一准确的时刻，而措辞会变。
存成文档，飞书层渲染。
"""

from tiny_hermes.channels.domain.command_receipt import (
    CommandReceipt,
    receipt_from_document,
)


def test_a_receipt_survives_the_round_trip() -> None:
    receipt = CommandReceipt(
        command="undo",
        outcome="done",
        messages=2,
        turns=1,
        echoed_text="图里是什么",
        busy_reason=None,
    )

    assert receipt_from_document(receipt.document()) == receipt


def test_a_busy_receipt_keeps_which_kind_of_busy() -> None:
    receipt = CommandReceipt(
        command="new",
        outcome="busy",
        messages=0,
        turns=0,
        echoed_text="",
        busy_reason="queued",
    )

    read_back = receipt_from_document(receipt.document())
    assert read_back is not None
    assert read_back.busy_reason == "queued"


def test_nothing_is_not_a_receipt() -> None:
    assert receipt_from_document(None) is None
    assert receipt_from_document({}) is None
