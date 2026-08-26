"""一条命令做成了什么，存成事实而不是句子。

和 `blocked.py` 同一个理由：入站那一刻是唯一准确的时刻。撤回之后队列会变、
措辞会改，但「你刚撤掉了 1 轮、2 条消息」在它发生时是真的，那才是要发给
用户的东西。渲染成飞书里的一句话属于 infrastructure。
"""

from dataclasses import dataclass
from typing import Any

from tiny_hermes.channels.domain._json import int_at, string_at


@dataclass(frozen=True)
class CommandReceipt:
    command: str
    #: `done` | `nothing` | `busy`
    outcome: str
    messages: int
    turns: int
    #: 被撤的那条 user 消息原文。回显它是为了让用户改一改重发，而不是
    #: 让他自己回忆刚才打了什么。
    echoed_text: str
    #: 只在 `outcome == "busy"` 时有值。`running`、`queued`、`parked`、
    #: `cancel_failed` 要说四句不同的话：等它跑完、前面还排着、停着等人处理
    #: （发 `/new` 出去），以及那一轮没能取消所以什么都没撤。
    busy_reason: str | None
    #: `/new` 顺手结束掉的、还没跑完的 Run 数。它们永远不会再答复，所以必须
    #: 说出来——否则用户只会发现自己有条消息石沉大海，而他刚被告知这是一段
    #: 全新的对话，正好没有理由去找。默认 0 让这个字段之前存下的回执照样读得回来。
    runs_ended: int = 0

    def document(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "outcome": self.outcome,
            "messages": self.messages,
            "turns": self.turns,
            "echoed_text": self.echoed_text,
            "busy_reason": self.busy_reason,
            "runs_ended": self.runs_ended,
        }


def receipt_from_document(document: dict[str, Any] | None) -> CommandReceipt | None:
    """没有回执和空回执是同一件事：这条事件不是命令。"""
    if not document:
        return None
    command = string_at(document, "command")
    outcome = string_at(document, "outcome")
    if not command or not outcome:
        return None
    return CommandReceipt(
        command=command,
        outcome=outcome,
        messages=int_at(document, "messages", 0),
        turns=int_at(document, "turns", 0),
        echoed_text=string_at(document, "echoed_text") or "",
        busy_reason=string_at(document, "busy_reason") or None,
        # 这个字段之前存下的回执没有这个键，读回来当 0——那些确实一个 Run 都
        # 没结束过。
        runs_ended=int_at(document, "runs_ended", 0),
    )
