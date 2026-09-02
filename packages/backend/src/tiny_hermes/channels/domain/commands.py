"""聊天里输入的命令，认出来或者不认。

只认**整条消息精确匹配**的几个名字。其它以 `/` 开头的一律不认，原样交给模型
——渠道里一条路径、一个日期、一句玩笑以 `/` 开头太常见，把它们吞掉的代价
（消息看起来石沉大海）远大于漏认一条命令。

纯函数，无 I/O：命令是什么，与谁发的、会话在哪、能不能执行无关。
"""

from dataclasses import dataclass
from enum import StrEnum


class CommandName(StrEnum):
    UNDO = "undo"
    NEW = "new"
    COMPACT = "compact"


@dataclass(frozen=True)
class ChatCommand:
    name: CommandName
    #: 只对 `UNDO` 有意义；`NEW` 和 `COMPACT` 永远是 1，因为它们不接受参数。
    turns: int = 1


_NAMES: dict[str, CommandName] = {
    "/undo": CommandName.UNDO,
    "/new": CommandName.NEW,
    "/reset": CommandName.NEW,
    "/compact": CommandName.COMPACT,
    "/compress": CommandName.COMPACT,
}


def parse(text: str, *, has_images: bool = False) -> ChatCommand | None:
    """整条消息是不是一条命令。

    `has_images` 让一条配图的 `/undo` 不是命令：带附件的消息几乎总是在说别的事，
    而误撤是不可见的——用户不会知道自己刚丢了一轮对话。
    """
    if has_images:
        return None
    words = text.strip().split()
    if not words:
        return None
    name = _NAMES.get(words[0].lower())
    if name is None:
        return None
    if name in (CommandName.NEW, CommandName.COMPACT):
        # 都不收参数。`/undo` 的参数是「撤几轮」，语义清楚；压缩没有对应的自然
        # 参数——「压到多少」不是发消息的人能判断的东西。多带一个词就不认，
        # 免得 `/compact 一下` 被当成命令、而用户以为自己给了个说明。
        return ChatCommand(name=name) if len(words) == 1 else None
    if len(words) == 1:
        return ChatCommand(name=name)
    if len(words) > 2:
        return None
    # `isascii()` 和 `isdecimal()` 都要，`isdigit()` 一个都不够：`'²'.isdigit()`
    # 为真而 `int('²')` 抛 ValueError——从 `run_for` 一路抛出去就是 webhook 500、
    # claim 回滚、飞书重投六小时。而 `'٣'` 两个谓词都为真且 `int` 收得下，只是
    # 没有人会用阿拉伯-印度数字给 `/undo` 传轮数，当成命令收下的风险大于收益。
    if not (words[1].isascii() and words[1].isdecimal()) or int(words[1]) < 1:
        return None
    return ChatCommand(name=name, turns=int(words[1]))
