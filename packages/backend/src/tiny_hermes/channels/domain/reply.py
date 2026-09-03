"""What a channel says back when a Run reaches its end.

The words, not the sending. Feishu's wire format belongs with the adapter
that talks to Feishu and cannot be checked from here without a tenant;
choosing what a person is told is this platform's own decision, and it is
the half a test can hold.

Written in Chinese because the surface is: a Feishu tenant's users read the
reply, and the console beside it is Chinese throughout. Anything an
operator reads instead — `reply_note`, the logs — stays in English with the
rest of the platform's machinery.
"""

from collections.abc import Mapping
from typing import Any

from tiny_hermes.runs.domain.models import TERMINAL_STATES, RunPurpose, RunState

#: This platform's cap, not a measured vendor limit. Feishu documents a size
#: limit on message content that this repository has not verified against
#: the service, so the number is chosen for the reader: a chat window is not
#: where a hundred-kilobyte transcript belongs. Truncating beats refusing —
#: a refused reply is a person who gets nothing at all.
MAX_REPLY_CHARS = 4000

_TRUNCATED = "\n\n（内容过长，已截断。完整结果请在控制台查看该 Run。）"

_NOTHING_SAID = "这次运行完成了，但没有产生可发送的文本。完整过程请在控制台查看该 Run。"
_CANCELLED = "这次运行已被取消。"
_FAILED = "这次运行失败了。"


def reply_for(
    *,
    state: RunState,
    said: str,
    failure_reason: str | None = None,
    purpose: RunPurpose = RunPurpose.ANSWER,
    compaction: Mapping[str, Any] | None = None,
    compaction_skipped: Mapping[str, Any] | None = None,
) -> str | None:
    """The reply, or `None` while there is nothing to say yet.

    `None` means "not finished", never "nothing to send". The dispatcher
    leaves those rows alone and comes back next scan; a non-terminal Run
    answering with words would both interrupt the person mid-work and stamp
    the delivery replied, so the real answer would never arrive.
    """
    if state not in TERMINAL_STATES:
        return None
    if state is RunState.CANCELLED:
        return _CANCELLED
    if purpose is RunPurpose.COMPACTION and state is RunState.COMPLETED:
        # `/compact` 的那种 Run：它从不回答问题，所以 `said` 永远是空。要发给
        # 人的是压缩的结果本身，而这时候压缩已经做完了——措辞用完成时，不用
        # 「记下了」那种把动作说成计划的话。
        if compaction_skipped is not None:
            # 平台主动没压：能合并的历史比摘要本身还短，压了反而更长，所以
            # 一次模型调用都没花。和下面那句分开，是因为「稍后再试一次」对
            # 这一种是假话——再试同样不会成，只会再白花一次摘要调用。
            return "这次没有压缩：能合并的内容太少，合并成摘要反而会更长。消息都还在。"
        if compaction is None:
            # 剩下的一种：压缩本身没成（比如摘要模型不可用）。不报「已压缩」
            # （假话），也不报「省了 0」（读起来像成功）。
            return "这次没能压缩成功，消息都还在。稍后再试一次。"
        covered = compaction.get("covered")
        # 报条数，不报省下多少。`freed_estimate` 来自 `estimate_tokens`，那个
        # 函数第一句就写着「An upper bound... A bound rather than a count」——
        # 它是给规划用的上界，故意往高了算，免得算出来装得下、实际发过去超窗。
        # 用在「该不该压」上完全正确，报给人看就不是了：2026-09-03 对过一次真机
        # 数据，回执说「少发 3089 个 token」，provider 数同一批内容是 1605（还
        # 含提示词），报出去的是真实值的两倍多。把一个设计上就偏高的规划上界
        # 当成测量值，比不报更糟。
        #
        # `covered` 照报：它是数出来的，不是估的。
        #
        # 措辞不用「一轮」「token」「上下文」。收到这句话的是飞书里的普通用户，
        # 上一版原话是「已压缩。合并了 23 条旧消息。每一轮大约少发 3089 个
        # token。」，用户的回应是「我真没看懂」。这句话要说的事其实很朴素：
        # 模型不记事，每次说话都要把整段历史重发一遍，合并之后就不必重发那一段。
        #
        # 「原文没有删除」这半句不是客套：只说「已压缩」读起来像是把那些消息
        # 删掉了，而它们都还在。
        if not isinstance(covered, int):
            return "已经压缩过了。之前的消息合并成了一份摘要，原文没有删除。"
        return (
            f"已经压缩过了。之前的 {covered} 条消息合并成了一份摘要，"
            "以后每次回复都不用再把它们重发一遍，会更快也更省。原文没有删除。"
        )
    if state is RunState.FAILED:
        # §-level rule of this repository rather than of the spec:
        # `failure_reason` is on its own list of fields written and never
        # rendered. A reply that said only "失败了" would put it back there.
        return _FAILED if not failure_reason else f"{_FAILED}原因：{failure_reason}"

    words = said.strip()
    if not words:
        return _NOTHING_SAID
    return _truncated(words)


def _truncated(words: str) -> str:
    """Cut, and say so.

    A truncation the reader cannot see reads as an Agent that stopped
    mid-sentence, which is a worse lie than a long message.
    """
    if len(words) <= MAX_REPLY_CHARS:
        return words
    return words[: MAX_REPLY_CHARS - len(_TRUNCATED)] + _TRUNCATED


#: How each unreadable type is named to the person who sent it. A type not
#: listed falls back to a general sentence — a new Feishu message type is a
#: reason to say less, never to guess.
_KINDS = {
    "image": "图片",
    "audio": "语音",
    "media": "视频",
    "file": "文件",
    "sticker": "表情",
    "post": "富文本",
    "share_chat": "群名片",
}


def refusal_for(kind: str) -> str:
    """What to say when a message arrives in a form this build cannot read.

    §19.2 forbids swallowing a message quietly, and this path used to do
    exactly that: a 200, a log line, and nothing for the person holding the
    phone. Saying "I can only read text" is a small thing to send, and it is
    the difference between a platform that is limited and one that is broken
    — from the outside those look identical until somebody says which.
    """
    named = _KINDS.get(kind)
    what = f"{named}消息" if named is not None else "这种类型的消息"
    return f"我暂时还处理不了{what},目前只能读文字。麻烦你用文字再说一遍。"


def progress_note() -> str:
    """What to say when a Run is taking a while.

    Says nothing about *what* it is doing, on purpose. That is tool names
    and internal state, which §19.1 keeps off an end-user surface — and a
    person waiting does not need the step list, they need to know the thing
    is alive. Sent exactly once, so it cannot become a chat that scrolls
    itself.
    """
    return "还在处理,这条要花点时间,完成后我会把结果发过来。"


__all__ = ["MAX_REPLY_CHARS", "progress_note", "refusal_for", "reply_for"]
