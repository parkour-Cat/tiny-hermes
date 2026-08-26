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
    #: 只在 `outcome == "busy"` 时有值。`running` 与 `queued` 要说不同的话：
    #: 一个是等它跑完，一个是前面还有别的消息排着。
    busy_reason: str | None

    def document(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "outcome": self.outcome,
            "messages": self.messages,
            "turns": self.turns,
            "echoed_text": self.echoed_text,
            "busy_reason": self.busy_reason,
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
    )
