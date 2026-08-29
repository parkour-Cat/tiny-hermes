"""交给辅助模型的那段话。

放在领域层是因为它决定摘要里有什么，而摘要是下一轮上下文的一部分——它和预算
表一样属于「这一轮发送什么」的规则，不属于某个 provider 的调用细节。
"""

_SECTIONS = (
    "目标：用户想达成什么",
    "约束与偏好：风格、口径、明确说过的限制",
    "进展：已完成 / 进行中 / 被阻塞",
    "已作出的决定：连同理由",
    "涉及的对象：文件、资源、外部系统，附一句它们各自的状态",
    "下一步：接下来要做的事",
    "关键事实：具体的值、报错、配置",
)


def summary_prompt(transcript: str, previous: str | None) -> str:
    head = (
        "更新下面这份既有摘要，使它覆盖新增的对话。保留仍然成立的条目，"
        "删掉已经过时的，不要从头重写。"
        if previous
        else "把下面这段对话压成一份结构化摘要。"
    )
    # Same truthiness test as `head`'s, not `is None`: the two must agree on
    # what "no previous summary" means, or an empty string would open with
    # the fresh form's wording while still appending an empty "既有摘要："
    # section underneath it — a shape nothing in this codebase ever sends
    # (`_generate_summary` never saves an empty summary, so a real `previous`
    # is always `None` or non-empty), but a pure function's two branches
    # should not disagree about a case it can still be called with.
    body = "" if not previous else f"\n\n既有摘要：\n{previous}"
    return (
        f"{head}\n\n"
        f"按这几节输出，没有内容的一节写「无」：\n"
        + "\n".join(f"- {s}" for s in _SECTIONS)
        + "\n\n"
        # 2026-08-26 的事故：模型在图片管道故障期间说过五次「我看不到图」，
        # 管道修好后它把那些当成已确认的事实继续拒绝。把那种话蒸馏进摘要会让它
        # 更短、更权威、也更难推翻。这一句能减轻，**不能消除**——摘要模型读到的
        # 仍然是那些话，它没有任何依据判断哪句是故障产物。
        "只记录发生了什么，不要记录助手声称的能力状态；"
        "助手说过自己做不到某事，不等于它做不到。\n\n"
        f"对话：\n{transcript}{body}"
    )
