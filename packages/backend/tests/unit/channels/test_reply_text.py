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
    """`/compact` 的回执说已经压完了，不是一句「记下了」。

    这是这条命令存在的理由：用户要的是「压完了」，而不是「排上队了」。

    **报条数，不报省下多少 token。**`freed_estimate` 来自 `estimate_tokens`，
    而那个函数的 docstring 第一句就是「An upper bound... A bound rather than a
    count」——它是给规划用的上界，故意往高了算，免得算出来装得下、实际发过去
    超窗。2026-09-03 拿真机数据对过一次：一次压缩报「少发 3089 个 token」，
    provider 数同一批内容是 1605（这个数还含提示词），**回执上那个数是真实值
    的两倍多**。把一个设计上就偏高的规划上界当成测量值报给人，比不报更糟。

    条数不一样：`covered` 是数出来的，不是估的。

    断言里两条「不含」：不许出现「已记下」那种把动作说成计划的措辞（压缩这时候
    已经做完了），也不许出现 `freed_estimate` 那个数。
    """
    said = reply_for(
        state=RunState.COMPLETED,
        said="",
        purpose=RunPurpose.COMPACTION,
        compaction={"covered": 12, "freed_estimate": 8400, "source": "model"},
    )
    assert said is not None
    assert "12" in said
    assert "记下" not in said
    assert "8400" not in said and "8,400" not in said, said


def test_a_finished_compaction_is_readable_by_someone_who_never_heard_of_a_token() -> None:
    """收到这句话的是飞书里的普通用户，不是读过 §7.4.2 的人。

    原来那句是「已压缩。合并了 23 条旧消息。每一轮大约少发 3089 个 token。」
    用户看完的原话：「我真没看懂」。「一轮」和「token」都是这套系统内部的词，
    而这句话要解释的事其实很朴素——模型不记事，每次说话都要把整段历史重发一遍，
    合并之后就不用重发那一段了。

    所以断言的是**不出现内部词汇**。

    这里曾经还断言过「原文没有删除」那半句在。用户看了改完的版本，说的是「太啰嗦
    了」——一句回执要说的是发生了什么，不是把每一层顾虑都写进去；「合并成一份
    摘要」本身已经不读作「删掉了」。措辞由产品定，这条断言随它一起去掉。
    """
    said = reply_for(
        state=RunState.COMPLETED,
        said="",
        purpose=RunPurpose.COMPACTION,
        compaction={"covered": 23, "freed_estimate": 3089, "source": "model"},
    )
    assert said is not None
    for jargon in ("token", "一轮", "上下文", "摘要模型"):
        assert jargon not in said, f"回执里出现了内部词汇「{jargon}」：{said}"
    # 短。一句回执在聊天窗口里是一行字，不是一段说明。
    assert len(said) <= 30, said


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
