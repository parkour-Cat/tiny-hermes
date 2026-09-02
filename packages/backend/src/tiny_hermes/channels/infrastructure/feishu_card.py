"""`BlockedNotice`, rendered as something a person can read in Feishu.

`channels/domain/blocked.py` said this belonged here — the facts §497
requires are checkable without a tenant, the vendor's card JSON is not.

What this replaces is silence. The notice already reached the webhook's
HTTP response body, which Feishu's server discards the moment it reads the
200, so every fact §497 demands was computed, serialized, and seen by
nobody. §19.2's `不能静默吞入新消息` was false the whole time.

Chinese, like `domain/reply.py` and for the same reason: a Feishu tenant's
users read this. English stays in `reply_note` and the logs, which are for
operators.
"""

from typing import Any

from tiny_hermes.channels.domain.blocked import BlockedNotice
from tiny_hermes.channels.domain.command_receipt import CommandReceipt

#: The head states a person can be told about, in their words rather than
#: the state machine's. A state missing from here falls back to a general
#: sentence: a new state is a reason to say less, never to say something
#: this build cannot actually know.
_HEAD_STATUS = {
    "waiting_approval": "前一个任务正在等人审批",
    "waiting_external": "前一个任务正在等外部系统回应",
    "paused": "前一个任务被暂停了",
    "cancelling": "前一个任务正在取消",
}

_PAUSE_REASON = {
    "manual": "有人手动暂停的",
    "limit": "触到了预算或用量上限",
    "compat_timeout": "兼容接口等待超时",
}

#: Action slugs as a person would say them. An unknown slug is shown as
#: itself rather than dropped — dropping it would quietly shorten the list
#: §497 requires, and a slug reads badly but tells the truth.
_ACTIONS = {
    "approve": "批准",
    "reject": "拒绝",
    "resume": "继续",
    "pause": "暂停",
    "cancel": "取消",
    "retry": "重试",
    "raise_ceiling": "提高预算上限",
}


def _card(
    title: str, template: str, elements: list[dict[str, Any]]
) -> dict[str, Any]:
    """The shell every card in this module shares.

    `update_multi` is the load-bearing part. Feishu requires it in `config`
    **both when the card is sent and when it is updated**; without it on the
    original send, every later patch is refused and the person is left
    looking at 「正在处理」 for the rest of the conversation. It lives here
    rather than in each renderer so a card added later cannot be written
    without it — the cost of forgetting is paid only against a real tenant.
    """
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "elements": elements,
    }


def _paragraph(content: str) -> dict[str, Any]:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def working_card() -> dict[str, Any]:
    """What a person sees about a second after they hit send.

    This card exists because a text message cannot be taken back. The
    platform used to stay silent until it knew what to say — 8 seconds for
    a progress note, longer for an answer — and silence is indistinguishable
    from a message that was dropped. A card can be sent immediately and
    rewritten in place once there is something to say.
    """
    return _card(
        "正在处理",
        "blue",
        [_paragraph("**收到了,正在处理。**\n\n有结果我会直接更新这条消息。")],
    )


def progress_card() -> dict[str, Any]:
    """The opening card, rewritten once when a Run turns out to be slow.

    Deliberately vague about *what* it is doing: that is tool names and
    internal state, which §19.1 keeps off an end-user surface. The person
    needs to know it is alive, not the step list.
    """
    return _card(
        "还在处理",
        "blue",
        [_paragraph("**还在处理,这条要花点时间。**\n\n完成后我会更新这条消息。")],
    )


def answer_card(text: str) -> dict[str, Any]:
    """The finished answer, replacing `working_card` in the same message."""
    return _card("回复", "green", [_paragraph(text)])


def failure_card(reason: str | None) -> dict[str, Any]:
    """A Run that failed, and why.

    `failure_reason` is on this project's own list of things written and
    never rendered. Carried here rather than flattened into "出错了",
    because the reason is the only part somebody can act on.
    """
    said = "这次运行失败了。"
    if reason:
        said = f"{said}\n\n原因:`{reason}`"
    return _card("运行失败", "red", [_paragraph(said)])


#: 回显截断到这里。照上游 Hermes 的 200 字符——够认出是哪条消息，
#: 又不至于把一条长提示整段抄回聊天窗口。
_ECHO_LIMIT = 200


def command_receipt_text(receipt: CommandReceipt) -> str:
    """一条命令的结果，说给发它的人听。

    `CommandReceipt` 存的是事实，措辞在这里才决定——和 `blocked_card` 读
    `BlockedNotice` 是同一层分工。`busy` 先于具体命令判断，因为「还在跑」
    「在排队」这两句话不因为命令是 `/undo` 还是 `/new` 而不同。

    `nothing` 不能这样合并：对 `/undo` 它是「没什么可撤」，对 `/new` 它是
    「你已经在一段新对话里了」。同一句话会让发 `/new` 的人以为命令失败了，
    于是再发一次。
    """
    if receipt.outcome == "busy":
        if receipt.busy_reason == "running":
            return "还有一轮在跑。等它结束，或者先取消，再试一次。"
        if receipt.busy_reason == "parked":
            # 只有 `/undo` 会读到这一句：`/new` 撞上停住的队首会把它取消掉，
            # 走的是 `done`。这句话给出的出口和阻塞卡片给的是同一个。
            return "前面有一轮停着在等人处理。可以发 /new 开始一段新对话。"
        if receipt.busy_reason == "cancel_failed":
            return "前面那一轮没能取消，所以这次什么都没撤。请到控制台处理后再试。"
        return "前面还有消息在排队。等队列走完再试一次。"
    if receipt.command == "compact":
        # 说条件句，不说承诺，也不说「已压缩」。压缩要一次模型调用，而入站路径
        # 不做外部调用（和 `blocked_notice` 的「在这里记下、不在这里发」同一条
        # 理由），所以这条命令做的是打标记，压缩发生在下一轮真正开始前。
        #
        # 「已压缩」是假话；「下次一定会压缩」是个不一定兑现的承诺——历史不够长
        # 时没有可压缩的内容，那时什么也不会发生，而用户已经被告知它会。
        if receipt.outcome == "nothing":
            # 对 `/compact`，「没什么可压」是一句让人放心的话，不是失败。和
            # `/undo` 的同名结果合并会让人以为命令没生效，于是再发一次。
            return "这段对话还不长，暂时没有需要压缩的历史。"
        return "记下了。下一条消息之前，会先把这段对话的旧历史压成摘要。"
    if receipt.command == "new":
        if receipt.outcome == "nothing":
            return "已经是一段新对话了，直接说就行。"
        said = "已经开始一段新对话。"
        if receipt.messages:
            said += f"之前的 {receipt.messages} 条消息不再进入上下文。"
        if receipt.runs_ended:
            # 被结束的 Run 永远不会再答复。不说出来，用户只会发现自己有条消息
            # 石沉大海，而他刚被告知这是一段全新的对话，正好没有理由去找。
            said += f"另外结束了 {receipt.runs_ended} 个还没跑完的任务，它们不会再有答复。"
        return said
    if receipt.outcome == "nothing":
        return "没有可撤的内容。"
    echoed = receipt.echoed_text
    if len(echoed) > _ECHO_LIMIT:
        echoed = echoed[:_ECHO_LIMIT] + "..."
    head = f"已撤回 {receipt.turns} 轮，共 {receipt.messages} 条。"
    return f"{head}\n\n你刚才说的是：\n{echoed}" if echoed else head


def blocked_card(
    notice: BlockedNotice, *, console_url: str | None = None
) -> dict[str, Any]:
    """The status card §19.2 requires, as Feishu's `interactive` content.

    `console_url` is `None` on a deployment that has not been told its own
    console address, and then no button is rendered. A guessed URL would be
    a dead link in front of a user, which is worse than no button — the
    address of a private deployment's console is known only to whoever
    deployed it.
    """
    lines = [
        f"**你的消息已经收到,正在排队。**目前排在第 {notice.position} 位。",
        f"原因:{_why(notice)}",
        _what_you_can_do(notice),
    ]
    elements: list[dict[str, Any]] = [_paragraph("\n\n".join(lines))]
    if console_url is not None:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "在控制台查看"},
                        "url": console_url,
                        "type": "primary",
                    }
                ],
            }
        )
    return _card("消息已排队", "orange", elements)


def _why(notice: BlockedNotice) -> str:
    """The head's state in a reader's words, with the specific reason when
    the platform recorded one.

    "Waiting" and "waiting for an approval nobody has answered" are
    different things to tell a person — which is why `BlockedNotice` keeps
    `head_status` and `pause_reason` apart rather than collapsing them.
    """
    said = _HEAD_STATUS.get(notice.head_status or "", "前一个任务还没有结束")
    reason = _PAUSE_REASON.get(notice.pause_reason or "")
    if reason is not None:
        return f"{said}({reason})。"
    if notice.wait_kind:
        return f"{said}(等待:{notice.wait_kind})。"
    return f"{said}。"


def _what_you_can_do(notice: BlockedNotice) -> str:
    """What this person may do, and — when there is something — where.

    Empty must not render as nothing: it means somebody else has to act,
    and a card that dropped the section would leave the reader with a queue
    notice and no idea whether they were expected to do something.

    Empty is **not** the usual case, though an earlier version of this
    docstring said so. It reasoned that a Feishu user is an `EndUser` and
    never a workspace member (§122), so `can_control` would be false — and
    a live tenant returned `resume`/`cancel`, because the blocked head was
    that same person's own Run and they could of course control it. The
    claim was reasoned rather than observed, and the observation contradicted
    it.

    Naming an action obliges this card to say where it can be taken. This
    build renders no interactive buttons — a real card-action callback needs
    its own webhook route, signature check and authorization — so a card
    that said `你现在可以:继续、取消。` offered two things it had no way
    to do. That is the comment rule in CLAUDE.md aimed at a string a person
    reads.
    """
    if not notice.available_actions:
        said = "这个需要有权限的人去处理,你先等着就行,前面结束后会自动开始。"
    else:
        named = "、".join(
            _ACTIONS.get(action, action) for action in notice.available_actions
        )
        said = f"前一个任务可以{named}——这些操作要在控制台里做。"
    # §935 要求阻塞卡片给出「新建会话」入口。这个 build 不渲染交互按钮
    # （原因见本函数上方 docstring），所以入口是一条命令而不是一个按钮 ——
    # 这句话就是那个入口本身，删掉它 §935 就没有实现了。
    return f"{said}\n\n被卡住时，可以发 /new 开始一段新对话。"


__all__ = [
    "answer_card",
    "blocked_card",
    "command_receipt_text",
    "failure_card",
    "progress_card",
    "working_card",
]
