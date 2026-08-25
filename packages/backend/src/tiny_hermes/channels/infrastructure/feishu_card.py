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
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(lines)}}
    ]
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
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "消息已排队"},
            "template": "orange",
        },
        "elements": elements,
    }


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
        return "这个需要有权限的人去处理,你先等着就行,前面结束后会自动开始。"
    named = "、".join(_ACTIONS.get(action, action) for action in notice.available_actions)
    return f"前一个任务可以{named}——这些操作要在控制台里做。"


__all__ = ["blocked_card"]
