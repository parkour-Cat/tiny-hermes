"""What a channel says back when a Run finishes.

The inbound half has been provable without a tenant since §574's claim
landed. This is the outbound half's provable part: given a terminal Run and
whatever the Agent said, decide the words. Sending them is the vendor's
business and is tested separately against a stub; *choosing* them is this
platform's own decision and belongs somewhere a test can pin it.

The case that matters most here is the one with no text. A Run that
completed having said nothing is not an error, and a channel that stays
quiet for it is indistinguishable — to the person who sent the message —
from a platform that dropped their message on the floor. That silence is
this repository's own recurring bug wearing a chat interface.
"""

import pytest
from tiny_hermes.channels.domain.reply import MAX_REPLY_CHARS, reply_for
from tiny_hermes.runs.domain.models import RunPurpose, RunState


def test_a_completed_run_replies_with_what_the_agent_said() -> None:
    assert reply_for(state=RunState.COMPLETED, said="上周有 12 单。") == "上周有 12 单。"


def test_a_completed_run_that_said_nothing_still_replies() -> None:
    """Silence is the failure mode, not the empty string.

    Whitespace counts as nothing: an Agent whose last turn was a newline has
    said nothing to a person, and stripping here rather than at the sender
    keeps that judgement in one place.
    """
    reply = reply_for(state=RunState.COMPLETED, said="  \n ")

    assert reply is not None
    assert reply.strip() != ""


def test_a_failed_run_says_so_and_says_why() -> None:
    """`failure_reason` is on this project's own list of things written and
    never rendered. A chat reply that omitted it would put it back."""
    reply = reply_for(
        state=RunState.FAILED, said="", failure_reason="model_endpoint_unreachable"
    )

    assert reply is not None
    assert "model_endpoint_unreachable" in reply


def test_a_failed_run_with_no_recorded_reason_still_says_it_failed() -> None:
    reply = reply_for(state=RunState.FAILED, said="")

    assert reply is not None
    assert reply.strip() != ""


def test_a_cancelled_run_says_so_rather_than_reporting_success() -> None:
    completed = reply_for(state=RunState.COMPLETED, said="做完了")
    cancelled = reply_for(state=RunState.CANCELLED, said="做完了")

    assert cancelled is not None
    assert cancelled != completed


@pytest.mark.parametrize(
    "state",
    [
        RunState.QUEUED,
        RunState.RUNNING,
        RunState.WAITING_APPROVAL,
        RunState.WAITING_EXTERNAL,
        RunState.PAUSED,
        RunState.CANCELLING,
        RunState.INTERRUPTED,
    ],
)
def test_a_run_that_has_not_finished_has_nothing_to_say_yet(state: RunState) -> None:
    """`None` means "not yet", never "nothing".

    The dispatcher treats `None` as leave-it-alone and comes back next scan.
    A non-terminal Run answering with words would send a person a reply in
    the middle of the work, and — worse — the dispatcher would stamp the
    delivery replied, so the real answer would never arrive.
    """
    assert reply_for(state=state, said="半句话") is None


def test_a_very_long_answer_is_cut_rather_than_refused() -> None:
    """The cap is this platform's, not a measured vendor limit.

    Feishu documents a size limit on message content that this repository
    has not verified against the service, so the number here is chosen for
    the reader rather than derived from the API: a chat window is not where
    a hundred-kilobyte dump belongs. Refusing to send would be worse than
    truncating — the person would get nothing at all.
    """
    reply = reply_for(state=RunState.COMPLETED, said="长" * (MAX_REPLY_CHARS * 3))

    assert reply is not None
    assert len(reply) <= MAX_REPLY_CHARS
    assert reply.startswith("长")
    # Cut, and *said* to be cut. A truncation the reader cannot see is a
    # reply that looks like the Agent stopped mid-sentence.
    assert reply.rstrip()[-1] != "长"


def test_a_finished_compaction_says_what_it_actually_did() -> None:
    """`/compact` 的回执带真实数字，不是一句「记下了」。

    这是这条命令存在的理由：用户要的是「压完了」，而不是「排上队了」。压缩 Run
    结束时，压了几轮、省了多少都在压缩事件里，回执照实说。

    断言里有「不含」的一条：不许出现「已记下」那种把动作说成计划的措辞——
    压缩这时候已经做完了。
    """
    said = reply_for(
        state=RunState.COMPLETED,
        said="",
        purpose=RunPurpose.COMPACTION,
        compaction={"covered": 12, "freed_estimate": 8400, "source": "model"},
    )
    assert said is not None
    assert "12" in said
    assert "8400" in said or "8,400" in said
    assert "记下" not in said


def test_a_compaction_that_compacted_nothing_says_so_instead_of_a_number() -> None:
    """压缩 Run 跑完却什么都没压——`compaction` 是 `None`。

    `/compact` 在建 Run 之前就挡掉了「没什么可压」，所以走到这里意味着压缩本身
    失败了（比如摘要模型不可用）。那时不能报「已压缩」，也不能报「省了 0」——
    前者是假话，后者读起来像成功。
    """
    said = reply_for(
        state=RunState.COMPLETED, said="", purpose=RunPurpose.COMPACTION, compaction=None
    )
    assert said is not None
    assert "已压缩" not in said


def test_a_compaction_with_nothing_to_gain_does_not_invite_a_retry() -> None:
    """压不出更小的上下文，和「压缩失败了」不是一回事，不能共用一句话。

    上一条测试原来的前提是「走到 `compaction is None` 只可能是摘要生成失败」，
    所以那句话以「稍后再试一次」收尾。现在多了第二种：能合并的历史比摘要本身
    还短，压了反而更长——这时候平台**主动不压**，一次模型调用都不花。

    对这一种说「稍后再试一次」是把一个确定的结论说成了偶然的故障：再试一次
    同样不会成，而且每一次都白花一次摘要调用——正是这次改动要省掉的那一笔。
    """
    said = reply_for(
        state=RunState.COMPLETED,
        said="",
        purpose=RunPurpose.COMPACTION,
        compaction=None,
        compaction_skipped={"reason": "no_gain", "covered": 2},
    )
    assert said is not None
    assert "已压缩" not in said
    assert "再试" not in said, said
