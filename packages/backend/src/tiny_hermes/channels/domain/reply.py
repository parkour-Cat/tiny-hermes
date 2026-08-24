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

from tiny_hermes.runs.domain.models import TERMINAL_STATES, RunState

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
    *, state: RunState, said: str, failure_reason: str | None = None
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


__all__ = ["MAX_REPLY_CHARS", "reply_for"]
